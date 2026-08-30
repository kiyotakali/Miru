"""Chat image upload is disabled for the first public build.

The product still uses the vision tier for screenshots. This test only guards
the manual chat-image entry points: web upload/paste/share and Android share
target registration.
"""
from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_chat_input_has_no_manual_image_upload_entrypoint():
    html = _read("templates/index.html")

    assert 'id="fileInput"' not in html
    assert 'id="fileLabel"' not in html
    assert 'id="fileName"' not in html
    assert "_pendingImageFileFallback" not in html
    assert "DataTransfer fallback" not in html
    assert "粘贴的图片" not in html
    assert "分享的图片" not in html


def test_send_companion_message_is_text_only():
    html = _read("templates/index.html")
    start = html.index("async function sendCompanionMessage()")
    end = html.index("let _companionLastMsgId", start)
    fn = html[start:end]

    assert "FormData" not in fn
    assert "append('image'" not in fn
    assert "URL.createObjectURL" not in fn
    assert "application/json" in fn


def test_android_apk_is_not_registered_as_image_share_target():
    manifest = _read("miru-mobile/android/app/src/main/AndroidManifest.xml")
    main = _read("miru-mobile/android/app/src/main/java/com/miru/companion/MainActivity.java")

    assert "android.intent.action.SEND" not in manifest
    assert 'android:mimeType="image/*"' not in manifest
    assert "getPendingShareImage" not in main
    assert "hasPendingShare" not in main
    assert "pendingShareDataUrl" not in main
