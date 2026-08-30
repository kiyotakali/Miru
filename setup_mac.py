"""py2app setup script — builds Miru.app for macOS menu bar.

Usage:
  pip install py2app
  python setup_mac.py py2app

The resulting .app will be in dist/Miru.app
"""
from setuptools import setup

APP = ["client_app.py"]
DATA_FILES = []
OPTIONS = {
    "argv_emulation": False,
    "plist": {
        "CFBundleName": "Miru",
        "CFBundleDisplayName": "Miru",
        "CFBundleIdentifier": "com.contextlife.miru",
        "CFBundleVersion": "0.1.0",
        "CFBundleShortVersionString": "0.1.0",
        "LSUIElement": True,  # menu bar app, no dock icon
        "NSAppleEventsUsageDescription": "Miru needs automation access.",
        "NSScreenCaptureUsageDescription": "Miru captures your screen to provide context-aware assistance.",
    },
    "includes": [
        "rumps",
        "PIL",
        "requests",
        "sensor",
        "device_manager",
    ],
    "packages": [
        "PIL",
        "requests",
        "certifi",
        "charset_normalizer",
        "idna",
        "urllib3",
    ],
    "excludes": [
        "flask",
        "anthropic",
        "openai",
        "httpx",
        "pywebview",
        "pynput",
        "pywebpush",
        "psycopg",
        "pytest",
    ],
    "iconfile": None,  # TODO: add Miru.icns
}

setup(
    app=APP,
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
