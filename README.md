# Hfoody backend

Why this exists: the HTML-only prototype hit three walls that a client-only
page can't get past —

1. API keys (OCR.space, Spoonacular) were sitting in the browser, visible to
   anyone who opens dev tools.
2. Israeli supermarket price/barcode data isn't a single API — each chain
   publishes its own files, in inconsistent formats, without CORS headers, so
   a browser can't fetch them directly.
3. Recipe/product lookups need caching and a real database instead of
   re-fetching from three different providers on every tap.

This service is a thin FastAPI app that fixes all three: it holds your keys,
runs the Israeli scraper/parser offline into a local SQLite file, and exposes
one stable API for the front-end to call.

## Honesty check, before you rely on this

I wrote this without network access in my own sandbox, so none of the outbound
calls (OCR.space, Spoonacular, the Israeli scraper) have been executed and
confirmed end-to-end. The code is careful and the libraries are real and
actively maintained, but library APIs and per-chain file formats do shift.
Treat this as a solid first draft: run it locally, watch the console output
of `refresh_data.py` closely the first time, and adjust the column-name
guesses in `load_into_sqlite()` if your output CSVs use different headers.

## Local setup

```bash
cd backend
python -m venv venv && source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# edit .env: paste your free OCR.space key, and Spoonacular key if you have one
uvicorn main:app --reload
```

Visit `http://localhost:8000/health` — should return `{"ok": true, ...}`.

## Building the Israeli product database (one-time, then on a schedule)

```bash
python refresh_data.py
```

This downloads price files for the chains listed in `ENABLED_SCRAPERS` inside
`refresh_data.py` (starts with just Shufersal — add more once that one works
for you), normalizes them, and writes `il_products.sqlite3`. Re-run this
periodically (daily is plenty) to keep prices fresh — a cron job, a scheduled
GitHub Action, or your host's built-in cron feature all work.

## API endpoints

- `GET /health`
- `POST /api/ocr` — form fields `image_base64` (data URL) and `language` (default `heb`)
- `GET /api/spoonacular/search?query=...&diet=vegan`
- `GET /api/il-products/search?barcode=...` or `?q=...`

## Getting to a live URL with the least manual work possible

Render deploys from a Git repo, so there's one unavoidable manual step —
getting this folder onto GitHub — everything after that is close to
one-click.

**Step 1 — push this folder to GitHub** (copy-paste, replace `YOUR_REPO_NAME`):

If you have the [GitHub CLI](https://cli.github.com/) installed (`gh`), this
one block does everything — creates the repo AND pushes, no website clicking:
```bash
cd backend
git init
git add .
git commit -m "Initial commit"
gh repo create YOUR_REPO_NAME --private --source=. --push
```
No `gh` CLI? Same result, one extra manual step (creating an empty repo on
github.com first, then):
```bash
cd backend
git init
git add .
git commit -m "Initial commit"
git remote add origin https://github.com/YOUR_USERNAME/YOUR_REPO_NAME.git
git branch -M main
git push -u origin main
```

**Step 2 — deploy.** Either:
- Click this button after replacing the placeholder with your repo's URL —
  it opens Render already pointed at your Blueprint, nothing to fill in:
  `https://render.com/deploy?repo=https://github.com/YOUR_USERNAME/YOUR_REPO_NAME`
- Or manually: Render dashboard → New → Blueprint → select your repo.

Either way, `render.yaml` already has working defaults baked in (see the file
itself), so there is no dashboard form to fill out — the free web service
just builds and comes up live at `https://YOUR_SERVICE_NAME.onrender.com`.

## Other hosts

Render's Blueprint above is the path with the least manual setup, but any of
these work too, all with a free or near-free tier:

- **Railway.app** — similar flow to Render, usage-based free credit monthly.
- **Fly.io** — `fly launch` picks up the Dockerfile automatically; free allowance covers a small always-on instance.

Whichever you pick: set `OCR_SPACE_KEY`, `SPOONACULAR_KEY`, and `ALLOWED_ORIGINS`
(your app's real domain, not `*`) as environment variables in the host's
dashboard — never commit `.env`.

## Getting this onto a phone (not just your computer)

`http://localhost:8000` only exists on the machine that's running it — a phone
on the same WiFi, let alone anywhere else, can't reach it. The front-end is
already built mobile-first (camera capture, bottom nav, RTL layout); what's
missing for phone use isn't the app, it's a public address. Two options:

**A. Quick test today, before deploying anywhere:**
```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```
Then find your computer's local IP (e.g. `192.168.1.23`) and open
`http://192.168.1.23:8000` from your phone — works only while your computer
is on and both devices share the same WiFi. Good for a five-minute test, not
for real use (and the camera may refuse to start over plain `http://` on some
phones — see the note below).

**B. Real deployment (what you actually want):**
Deploy to Render/Railway/Fly.io as described above. You get a permanent
`https://your-app.onrender.com` address that works from any phone, anywhere,
any time — no shared WiFi, no computer left running. This also matters for
the camera specifically: browsers only allow camera access on `https://` (or
`localhost`), so a real deploy is what makes barcode scanning reliable on a
phone in the first place.

**Installing it like an app:** once deployed, open the `https://` URL on the
phone, then:
- **Android (Chrome):** menu (⋮) → "Add to Home screen" / "Install app".
- **iPhone (Safari):** Share button → "Add to Home Screen".

Either way it now opens full-screen with its own icon, no browser chrome —
the `manifest.json` and icons already included in `frontend/` make this work.


## Connecting the front-end

If you deploy `frontend/` and the backend together (the setup above — backend
serves the frontend itself), nothing to configure: the app pings `/health` on
its own origin at startup and uses itself as the backend automatically.

Only if you ever split them — frontend hosted somewhere else, backend
elsewhere — open the app's profile menu (⚙) and set "כתובת שרת" (backend URL)
to the backend's address. Leave it blank otherwise.

