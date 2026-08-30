# Miru Brand Assets

Miru's current visual identity has two maintained sources:

- `miru-logo.svg` — app logo source: a line-art cat peeking from a window, with warm eyes and a bookmark accent.
- `miru-avatar.png` — default chat avatar: Q-style Miru with high ponytail, cream cardigan, and bookmark hair clip.

Run this after changing the logo:

```bash
PYTHONPATH=. .venv/bin/python scripts/generate_brand_assets.py
```

The script refreshes PWA icons, Tauri/macOS icons, and Android launcher assets.
