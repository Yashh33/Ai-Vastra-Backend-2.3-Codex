# Working rules for this repo

AI Vastra backend: FastAPI API (`main.py`), background worker (`worker.py`), WhatsApp bot
(`whatsapp_*.py`) and the admin panel (`adminPanel/`, Vite + React).

Production runs from the **`production`** branch on Render, with Auto-Deploy **OFF**.
Two Render services deploy from it: **"Ai Vastra Backend 2.3"** and **"Ai Vastra Worker 2.3"**.
The owner deploys manually (see `DEPLOY.md`). A second developer also pushes to `main`.

## Branches and deploys
- Work only on `main`.
- Never check out, merge into, or push to `production`. Never trigger a deploy.
- Never say a change is "live". Say: "pushed to main, goes live after merge + manual deploy".

## Git
- Run `git pull` before starting any task (someone else pushes to `main` too).
- Run `git status` before every commit.
- Stage only the files you changed: `git add <path>`. Never `git add .` or `git add -A`.
- Never stage: `*_latest*.txt` dump files, other local dumps (`adminPanel/adminpanel*.txt`,
  `worker_prompting_dump.txt`), `cleanup_storage.py`, or any `.env` file.

## Local runs use the LIVE database and storage
- Local runs connect to the same live Supabase database and storage as production.
- Every local backend run must have `WHATSAPP_WATCHER_ENABLED=false` in the local `.env`.
  Never start the backend locally with the WhatsApp watcher on: it polls real customers'
  sessions and would race production's watcher for them. Check the startup log shows
  `WhatsApp watcher disabled (WHATSAPP_WATCHER_ENABLED=false)`; if it shows
  `[whatsapp_watcher] started`, stop the server immediately.
- With that set, start the backend normally (`uvicorn main:app`); no special launcher is needed.
- Test only with the demo shop (the `demo@gmail.com` account).
- Never read or modify real shops' data.
- Never run scripts that delete storage objects or database rows.

## Database changes: add-only
- Applies to tables, columns, RLS policies, RPC functions and settings rows.
- New columns must be nullable or have a default. New functions get new names.
- Never rename, drop, or change the signature or behaviour of anything a live app uses.
  Removals happen only later, in a separate cleanup, after every app has stopped using it.
- Every SQL change goes in a new file `sql/YYYY-MM-DD_short_name.sql`.
- Do NOT run SQL yourself. The owner runs it. Flag every SQL file clearly in the report.

## Contract with live apps (never change or remove; only add new ones)
The Android TV app (installed APKs that cannot update instantly) and the web app call these
Supabase objects directly:
- RPCs: `resolve_shop_by_slug`, `get_shop_display_name`, `set_screen_mode`,
  `set_generation_hero`, `get_carousel_looks`, `get_browse_garment_types`,
  `get_browse_looks`, `get_browse_looks_page`
- Tables: `shop_screen_state`, `generations` (`id`, `output_path`)
- Storage: bucket `generated-outputs` and its thumbnail path rules
  (`image_thumbs.look_thumbnail_path`: `output*.ext` -> `thumb*.jpg` in the same folder)

## Backend API compatibility
- Deploy order is database -> backend + worker -> frontend. For a few minutes the OLD
  frontend talks to the NEW backend, so every API change must stay backward compatible:
  add fields and endpoints; never remove or rename them, and never make an optional input required.

## Environment variables
- New variable: add its name to `.env.example` (names only, never values) and list it in the report.
- Tell the owner to add it in Render BEFORE the deploy that needs it. Backend and worker
  share `config.py`, so add it to BOTH services if both read it.
- If production needs it, also add it to `_HEALTHZ_REQUIRED_ENV` in `main.py`.
- Never print, log or commit secret values.

## WhatsApp
- The Meta webhook always points at the live backend URL. Never change webhook settings or
  point them at a local URL.
- Never send WhatsApp messages to real customers while testing.
- The keyword `menu` is reserved (garment-types menu). Never reuse it.

## Safety rules
- `FAL_KEY` and service-role keys stay backend-only. Never send them to a frontend.
- Customer photos are never stored.
- Never send customer photos or try-on results to fal / SAM. Only generated looks and
  shop-uploaded garment/fabric photos may go there.
- No double-crediting: a generation is charged at most once and refunded at most once; never
  add a second debit or credit for the same event.
- Never disable RLS.

## End every task with a report
1. Commits pushed to `main` (hashes).
2. Database change: yes/no, plus the SQL file names to run.
3. New environment variables: yes/no, plus names and which Render service(s).
4. Anything the owner must do in Render / GitHub / Supabase / Meta.
5. Whether the frontend or the TV app needs a matching change.
