# Contributing

Contributions are welcome through GitHub issues and pull requests.

1. Create a focused branch from `main`.
2. Keep local single-device and private-server behavior isolated.
3. Add or update tests for behavioral changes.
4. Run `PYTHONPATH=. .venv/bin/python -m pytest tests/ -q`.
5. Run `bash scripts/scan_secrets.sh` before committing.

Do not include model API keys, SSH keys, invitation codes, user data, private
server addresses, generated packages, or third-party Live2D files. See
`docs/BUILDING.md` and `THIRD_PARTY_NOTICES.md`.

Changes to shared server behavior require regression checks on macOS, Windows,
Android, and Docker. UI changes should include screenshots for the affected
desktop and mobile viewports.
