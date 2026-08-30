# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the x64 Windows Miru desktop client."""

import os

from PyInstaller.utils.hooks import collect_all


ROOT = os.path.abspath(".")
webview_datas, webview_binaries, webview_hiddenimports = collect_all("webview")

a = Analysis(
    ["windows_launcher.py"],
    pathex=[ROOT],
    binaries=webview_binaries,
    datas=[
        ("templates", "templates"),
        ("assets", "assets"),
        ("icon-192.png", "."),
        ("icon-512.png", "."),
        ("manifest.json", "."),
        ("soul.md", "."),
        ("tools", "tools"),
        ("deploy/host_manager", "deploy/host_manager"),
        ("deploy/self_host", "deploy/self_host"),
        ("scripts/host_manager.py", "scripts"),
        ("src-tauri/icons/icon.ico", "src-tauri/icons"),
    ] + webview_datas,
    hiddenimports=[
        "webview",
        "webview.platforms.edgechromium",
        "clr",
        "pythonnet",
        "PIL",
        "PIL.Image",
        "PIL.ImageChops",
        "PIL.ImageGrab",
        "requests",
        "urllib3",
        "certifi",
        "charset_normalizer",
        "idna",
        "paramiko",
        "flask",
        "flask_compress",
        "werkzeug",
        "jinja2",
        "markupsafe",
        "qrcode",
        "qrcode.image",
        "qrcode.image.pil",
        "sensor",
        "device_manager",
        "screen_analyzer",
        "self_profile",
        "character",
        "storage",
        "core",
        "companion",
        "ai_config",
        "auth",
        "sse",
        "model_library",
        "prompt",
        "memory_prompts",
        "memory",
        "core_memory",
        "curator",
        "care_engine",
        "miru_emotion",
        "sleep_agent",
        "sync_backend",
        "server_config",
        "user_settings",
        "desktop_paths",
        "windows.platform",
        "app",
        "anthropic",
        "openai",
        "httpx",
        "pywebpush",
        "cryptography",
    ] + webview_hiddenimports,
    excludes=[
        "pytest",
        "matplotlib",
        "numpy",
        "scipy",
        "pandas",
        "tkinter",
        "psycopg",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Miru",
    icon="src-tauri/icons/icon.ico",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="Miru",
)
