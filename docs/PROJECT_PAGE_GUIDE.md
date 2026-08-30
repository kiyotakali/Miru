# Miru Project Page Collaboration Guide

This guide is for collaborators who want to edit the public project page at
`https://mirulife.top/`.

## Source File

- Main file: `templates/landing.html`
- Public route: Flask `GET /`
- App route: `/app` is the product UI and should not be changed for project-page
  copy/design work.
- Production URL: `https://mirulife.top/`
- Production runtime: the Flask service on the Miru VPS serves files from the
  deployed app directory, currently `/opt/miru`.
- Current visual assets:
  - `icon-192.png`
  - `icon-512.png`
  - `assets/brand/miru-avatar.png`
  - `assets/brand/miru-logo.svg`

For normal homepage copy/layout work, keep the PR scoped to
`templates/landing.html` and public assets. Do not mix homepage changes with
DMG/APK/Docker/provisioning code unless explicitly requested.

Important: pushing to GitHub is not the same thing as changing the live site
unless the production deploy workflow is configured and has run successfully.

## Product Truths To Preserve

- Miru currently supports two user-facing paths:
  - Local single-device mode on Mac.
  - Self-server multi-device mode with Mac + Android using the same long
    invitation code.
- The current public release assets are distributed through GitHub Releases:
  macOS DMG, Android APK, and `miru-server-v0.2.0-linux-amd64.tar.gz`.
- Model setup uses three OpenAI-compatible Chat Completions tiers:
  Vision, Chat, and Memory.
- The homepage should say recommendations are examples, not the only supported
  providers.
- Official hosted purchasing/provisioning is a future route, not the current
  primary public page flow.
- The public homepage does not store user chat, screenshots, memory, or API keys.

## Words And Directions To Avoid

Do not bring back old public-page language such as:

- `CareEngine`
- `Memory v2`
- "邀请制内测" as the primary status
- "内测暂未公开"
- central-backend-first wording
- "Miru always stays aside / does not disturb" as the emotional center

The current product direction is that Miru feels like a girl who cares, remembers,
and sometimes naturally approaches. She respects boundaries, but the homepage
should not make her sound timid or like a notification policy.

## Design Direction

The page should feel closer to a Japanese game / visual novel project page than
a SaaS dashboard:

- Miru is the first visual signal in the first viewport.
- Use Story / Life / Memory / Start / Download style sections.
- Keep a warm paper, sakura, ink, and character-forward feeling.
- Prefer concrete daily-life scenes over feature-card lists.
- Use real product screenshots or character visuals when possible.

Do not copy Atlus / Persona / galgame assets or layouts directly. Borrow the
page grammar: strong first viewport, character presence, chaptered reveal, and
story-world feeling.

## Local Preview

Run the Flask app locally and open `/`:

```bash
PYTHONPATH=. .venv/bin/python -c "from app import app; app.run(host='127.0.0.1', port=8767, debug=False, use_reloader=False)"
```

Then open:

```text
http://127.0.0.1:8767/
```

## Deployment

The production page is deployed to the VPS, not hosted directly from GitHub
Pages. The repository contains a narrow deploy script for homepage-only changes:

```bash
bash deploy/update_project_page.sh
```

This script uploads only:

- `templates/landing.html`
- `icon-192.png`
- `icon-512.png`
- `assets/brand/*`

Then it restarts the `miru` systemd service and verifies
`https://mirulife.top/`.

Default deployment target:

```text
MIRU_VPS_HOST is provided by maintainer environment or GitHub secret
MIRU_REMOTE_DIR=/opt/miru
MIRU_SERVICE_NAME=miru
```

Maintainers can override these with environment variables when deploying another
host:

```bash
MIRU_VPS_HOST=root@<server-ip> MIRU_REMOTE_DIR=/opt/miru bash deploy/update_project_page.sh
```

Use a dry run to see exactly what would be uploaded:

```bash
bash deploy/update_project_page.sh --dry-run
```

## GitHub Auto Deploy And Trusted Direct Deploy

`.github/workflows/deploy-project-page.yml` deploys homepage-only changes after
they are merged to `main`. It triggers when these paths change:

- `templates/landing.html`
- `icon-192.png`
- `icon-512.png`
- `assets/brand/**`
- `deploy/update_project_page.sh`
- `.github/workflows/deploy-project-page.yml`

The workflow needs these repository settings:

- Secret `MIRU_VPS_HOST`: SSH login string, for example `root@<server-ip>`.
- Secret `MIRU_VPS_SSH_KEY`: private SSH key that can log into the VPS.
- Optional variable `MIRU_REMOTE_DIR`: defaults to `/opt/miru`.
- Optional variable `MIRU_SERVICE_NAME`: defaults to `miru`.
- Optional variable `MIRU_PROJECT_PAGE_PUBLIC_URL`: defaults to
  `https://mirulife.top/`.

Trusted collaborators with repository `write` access can deploy in two ways:

1. Push project-page changes directly to `main`; the workflow deploys
   automatically.
2. Push a branch and manually run the `Deploy Project Page` workflow against
   that branch for a production preview.

The branch deploy command is:

```bash
gh workflow run "Deploy Project Page" --ref <branch-name> -f note="project page preview"
gh run watch --exit-status
```

After deploying a branch preview, merge or fast-forward `main` once the page is
accepted, so Git history matches production.

Fork PRs should not receive production SSH secrets. For external contributors,
the safer flow is:

1. Collaborator opens a PR with local preview screenshots and checks.
2. Maintainer reviews and merges to `main`.
3. GitHub Actions deploys the project page from `main`.
4. Maintainer verifies `https://mirulife.top/`.

If the GitHub secrets are not configured, collaborators can still edit and open
PRs, but the merge will not update the live page. A maintainer must either add
the secrets once or run `bash deploy/update_project_page.sh` locally.

For a Codex-specific handoff document, give the collaborator's agent:

```text
docs/PROJECT_PAGE_CODEX_COLLABORATOR.md
```

## Required Checks Before PR

Run the inline JavaScript syntax check:

```bash
node -e "const fs=require('fs'); const h=fs.readFileSync('templates/landing.html','utf8'); const scripts=h.match(/<script(?!.*src=)[^>]*>([\s\S]*?)<\/script>/g)||[]; let ok=true; scripts.forEach((s,i)=>{const c=s.replace(/<script[^>]*>|<\/script>/g,''); try{new Function(c)}catch(e){ok=false; console.error('script '+i+' FAIL: '+e.message)}}); console.log('checked '+scripts.length+' inline scripts'); if(!ok) process.exit(1);"
```

Run a lightweight Flask smoke:

```bash
PYTHONPATH=. .venv/bin/python - <<'PY'
from app import app
with app.test_client() as c:
    for path in ['/', '/icon-192.png', '/icon-512.png', '/assets/character-avatar.png']:
        r = c.get(path)
        assert r.status_code == 200, (path, r.status_code)
    html = c.get('/').data.decode('utf-8')
    for text in ['Miru', 'v0.2.0', 'OpenAI-compatible', '自有服务器', '本地单设备']:
        assert text in html, text
print('landing smoke ok')
PY
```

Also manually inspect at least:

- Desktop width around 1440px.
- Mobile width around 390px.
- Hero section.
- Download section.
- Text wrapping in buttons and cards.

## PR Expectations

In the PR description, include:

- What changed visually.
- What changed in copy.
- Desktop screenshot.
- Mobile screenshot.
- The checks you ran.
- Any intentional follow-up work.

Keep PRs small enough to review. A good PR changes one of:

- Hero / first viewport.
- One page section.
- Copy pass.
- Assets / screenshots.
- Download or FAQ wording.
