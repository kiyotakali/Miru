# Miru Project Page Collaboration Guide

This repository is the public source of truth for the Miru project page.

## Local Preview

```bash
git clone https://github.com/kiyotakali/Miru.git
cd Miru
python3 -m http.server 8765 -d .
```

Open:

```text
http://127.0.0.1:8765/?prologue=skip
```

Use `?prologue=1` to replay the opening prologue.

## File Layout

```text
index.html
_page/css/main.css
_page/js/main.js
_page/assets/
```

The page intentionally uses `_page/*` asset URLs. When served at the domain
root, browsers request them as `/_page/*`. Do not move assets back to
`/assets/*`: the Miru app backend already uses `/assets/live2d/*` and related
routes for app runtime resources.

## What To Edit

Usually edit only:

```text
index.html
_page/css/main.css
_page/js/main.js
_page/assets/*
README.md
```

Keep the page as a Japanese game / visual novel style project page, not a SaaS
feature-card landing page.

## Deployment

Pushing to `main` triggers `.github/workflows/deploy-project-page-vps.yml` when
these paths change:

```text
index.html
_page/**
scripts/deploy_project_page_vps.sh
.github/workflows/deploy-project-page-vps.yml
```

The workflow uploads this static page to the VPS external homepage directory:

```text
/opt/miru/project_page/releases/<github_sha>
/opt/miru/project_page/current -> releases/<github_sha>
```

The private Miru backend serves:

```text
/        -> /opt/miru/project_page/current/index.html
/_page/* -> /opt/miru/project_page/current/_page/*
```

If no external project page has been deployed, the backend falls back to its
bundled `templates/landing.html`.

## Maintainer-only GitHub Settings

Repository owner must configure these once:

Secrets:

```text
MIRU_PAGE_SSH_HOST
MIRU_PAGE_SSH_KEY
```

Optional variables:

```text
MIRU_PAGE_SSH_PORT      default 22
MIRU_PAGE_REMOTE_DIR    default /opt/miru/project_page
MIRU_PAGE_PUBLIC_URL    default https://mirulife.top/
```

Collaborators with write access do not need Settings access after this is
configured. They can push page changes and the workflow will deploy them.

## Basic Checks Before Push

```bash
python3 -m http.server 8765 -d .
```

Then verify:

```text
http://127.0.0.1:8765/?prologue=skip
```

Do not commit API keys, invite codes, SSH keys, or private server details.
