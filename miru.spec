# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for Miru Mac client — full packaging, client mode only.
#
# Build:
#   .venv/bin/pyinstaller miru.spec --clean --noconfirm
#
# Then inject Tauri pet:
#   cp src-tauri/target/release/app dist/Miru.app/Contents/MacOS/miru-pet

import os
ROOT = os.path.abspath('.')

a = Analysis(
    ['miru_launcher.py'],
    pathex=[ROOT],
    binaries=[],
    datas=[
        ('templates', 'templates'),
        ('assets', 'assets'),   # includes assets/live2d/Hiyori — bundled pet model
        ('icon-192.png', '.'),
        ('icon-512.png', '.'),
        ('manifest.json', '.'),
        ('soul.md', '.'),
        ('tools', 'tools'),
        ('deploy/host_manager', 'deploy/host_manager'),
        ('deploy/self_host', 'deploy/self_host'),
        ('scripts/host_manager.py', 'scripts'),
    ],
    hiddenimports=[
        # macOS event loop + invitation dialog
        'rumps', 'AppKit', 'Foundation', 'WebKit', 'objc',
        # Image processing (sensor)
        'PIL', 'PIL.Image', 'PIL.ImageChops',
        # HTTP
        'requests', 'urllib3', 'certifi', 'charset_normalizer', 'idna',
        # Password SSH for self-server first-run wizard
        'paramiko',
        # Flask (local /pet page serving)
        'flask', 'flask_compress', 'werkzeug', 'jinja2', 'markupsafe',
        # QR code
        'qrcode', 'qrcode.image', 'qrcode.image.pil',
        # Business modules (imported by app.py chain)
        'sensor', 'device_manager', 'screen_analyzer',
        'self_profile', 'character',
        'storage', 'core', 'companion', 'ai_config',
        'auth', 'sse', 'model_library',
        'prompt', 'memory_prompts', 'memory', 'core_memory',
        'care_engine', 'miru_emotion', 'sleep_agent',
        'sync_backend', 'app',
        # AI SDKs (lazy-imported but include for completeness)
        'anthropic', 'openai', 'httpx',
        # Push notifications
        'pywebpush', 'cryptography',
    ],
    excludes=[
        'pytest', 'matplotlib', 'numpy', 'scipy',
        'pandas', 'tkinter', 'psycopg',
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Miru',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    target_arch=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='Miru',
)

app = BUNDLE(
    coll,
    name='Miru.app',
    icon='src-tauri/icons/icon.icns',
    bundle_identifier='com.contextlife.miru',
    info_plist={
        'CFBundleName': 'Miru',
        'CFBundleDisplayName': 'Miru',
        'CFBundleVersion': '0.2.0',
        'CFBundleShortVersionString': '0.2.0',
        # Design C: DMG is a regular desktop app with a Dock icon.
        # Dock icon click re-opens the backend WebView window. The Tauri
        # pet child process inherits Regular activation from us and can
        # therefore display its floating Live2D window. (If we set
        # LSUIElement=True the Tauri child inherits agent mode and its
        # NSWindow silently never appears onscreen.)
        'LSUIElement': False,
        'NSScreenCaptureUsageDescription':
            'Miru captures your screen to provide context-aware AI assistance.',
        'NSAppTransportSecurity': {
            'NSAllowsArbitraryLoads': True,
            'NSAllowsArbitraryLoadsInWebContent': True,
        },
        'LSMinimumSystemVersion': '12.0',
        'NSHighResolutionCapable': True,
    },
)
