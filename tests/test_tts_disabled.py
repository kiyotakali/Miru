from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_chat_tts_autoplay_is_disabled_until_backend_exists():
    html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")

    assert "let TTS_AUTO_PLAY = false" in html
    assert "Voice output is not part of the first public build" in html
