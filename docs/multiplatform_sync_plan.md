# ContextLife Multi-Platform + Sync Plan

## 1. Goal

- Keep existing core unchanged as much as possible:
  - Backend: Flask + `core.py` + `storage.py`
  - Frontend: `templates/index.html`
- Produce app binaries for:
  - iOS
  - Android
  - macOS
  - Windows
  - Linux
- Maintain one web core and one shell code path for all platforms.

## 2. Current Multi-Platform Shell

This repo now includes a Tauri 2 shell:

- Config: `src-tauri/tauri.conf.json`
- Loader page: `tauri-shell/web/index.html`
- Shell URL config: `tauri-shell/web/config.json`
- URL switch helper: `scripts/set_shell_url.py`
- Build scripts: `package.json`

### Commands

```bash
# set shell target
npm run shell:url:prod
npm run shell:url:local

# desktop
npm run shell:dev
npm run shell:build

# android
npm run shell:android:init
npm run shell:android:dev
npm run shell:android:build

# ios
npm run shell:ios:init
npm run shell:ios:dev
npm run shell:ios:build
```

Notes:
- `shell:dev` uses `devUrl` from `src-tauri/tauri.conf.json` (`http://127.0.0.1:5001`).
- Build mode uses `tauri-shell/web/config.json` and redirects to `web_url`.

## 3. Prompt and Classification Optimization

Prompt rules were strengthened in `prompt.py` for:

- first-level then second-level classification discipline
- A1 person extraction from descriptor sentences
- explicit handling of pattern:
  - `"xxx是战神"` -> `A1` card, title=`xxx`, role/notes include `战神`

Updated prompt sections:
- `EVIDENCE_CARD_SYSTEM_PROMPT`
- `DERIVE_SUB_CARDS_SYSTEM_PROMPT`
- `PLAN_SUB_CARDS_SYSTEM_PROMPT`
- `GENERATE_SUB_CARD_SYSTEM_PROMPT`

## 4. High-Intensity Prompt Stress Testing

Added:
- cases: `tests/prompt_stress_cases.json`
- runner: `scripts/prompt_stress.py`

Usage:

```bash
# list selected cases only
python3 scripts/prompt_stress.py --dry-run

# full run
python3 scripts/prompt_stress.py --rounds 3 --shuffle

# run selected cases
python3 scripts/prompt_stress.py --cases a1_person_descriptor,b1_commitment
```

Runner flow:
- save message
- analyze pending
- confirm card
- verify expected category/title/content signals
- print pass/fail and overall rate

## 5. Multi-Device Sync Design (BYODB included)

### 5.1 Modes

1. Local-only mode (current default)
- JSON files under `DATA_DIR`
- best for single device

2. Managed cloud mode
- Metadata: PostgreSQL
- Large files: Object Storage (S3-compatible)

3. BYODB mode (user-owned database)
- User provides:
  - `SYNC_DATABASE_URL` (PostgreSQL)
  - `SYNC_OBJECT_STORE_ENDPOINT` / bucket / key / secret (optional but recommended)

### 5.2 Why DB + Object Store split

- Timeline/cards/metrics are structured records -> relational DB
- Images/uploads can be large -> object storage
- Keeps DB slim and sync faster

### 5.3 Suggested data model

Metadata tables:
- `timeline_entries`
- `evidence_cards`
- `sub_cards`
- `metrics_daily`
- `daily_reviews`
- `tomorrow_plans`
- `character_plans`
- `chat_messages`

Blob table:
- `attachments` (metadata only, points to object storage key)

### 5.4 Large file handling

- Upload flow:
  - client asks backend for pre-signed upload URL
  - client uploads directly to object storage
  - backend writes attachment metadata (hash, size, mime, key)
- Dedup:
  - hash-based dedup (`sha256`)
- Preview:
  - store compressed preview + original
- Cleanup:
  - reference counting + garbage collection job

### 5.5 Conflict strategy

- Include `updated_at` and `version`
- Server-side merge policy:
  - default: last-write-wins for simple fields
  - list fields: set-union merge
  - destructive operations: tombstone records

## 6. Recommended Implementation Sequence

1. Keep shell-first release:
- ship Tauri shell on desktop first

2. Add sync abstraction layer:
- preserve current JSON backend
- add DB backend behind the same storage interface

3. Introduce object storage for uploads:
- migrate `data/uploads` references to attachment keys

4. Enable BYODB setup UI:
- collect DB/object-store config from user
- validate connection and write health status

5. Run stress suite per release:
- prompt stress
- API regression
- reminder scheduling consistency

## 7. Implemented in this branch

- Added optional PostgreSQL sync backend: `sync_backend.py`
- Added sync APIs:
  - `GET /api/sync/config`
  - `POST /api/sync/config`
  - `POST /api/sync/test`
  - `GET /api/sync/status`
  - `POST /api/sync/push`
  - `POST /api/sync/pull`
  - `POST /api/sync/run`
- Added in-app sync settings entry in sidebar (`☁️`), no terminal required for mobile users
- Added optional background sync loop (controlled by `SYNC_INTERVAL_SECONDS`)
- Added CLI helper: `scripts/sync_db.py`
- Added large-file handling switch:
  - metadata-only mode (`SYNC_UPLOAD_INLINE=0`, default)
  - inline small-file mode with threshold (`SYNC_UPLOAD_INLINE=1` + `SYNC_UPLOAD_MAX_INLINE_MB`)
