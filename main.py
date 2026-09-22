"""
Hfoody backend.

Four jobs this service does that a client-only HTML page structurally cannot:
  1. Hold API keys server-side (OCR.space, Spoonacular, Google Vision) instead
     of shipping them to every visitor's browser.
  2. Serve a local database of Israeli supermarket products (barcode, name,
     price, chain) — pre-built by refresh_data.py, plus products supermarkets
     submit directly (see access tiers below).
  3. Give you one stable API surface to point the front-end at.
  4. Enforce three access tiers with real server-side checks (not just hiding
     buttons in the UI, which anyone could bypass by reading the page source):
       - Customer:  the app itself. No login — profile/preferences live in the
                    browser's own storage (window.storage), per device.
       - Supermarket: can submit products (barcode, name, price, ingredients)
                    via a shared access key. Simple by design — this is a
                    single shared password per deployment, not individual
                    per-store accounts with their own login. Fine for a
                    prototype / one pilot partner; genuinely multiple stores
                    needing separate logins and audit trails is a real,
                    separate build (proper accounts + hashed passwords + a
                    users table), not a header check.
       - Developer: read-only stats endpoint, gated by a second shared key.
    Both keys are compared with a constant-time check to avoid timing attacks,
    but they are still simple shared secrets — treat them like a password you
    hand to a trusted partner, not like per-user authentication.

Run locally:
    pip install -r requirements.txt
    cp .env.example .env   # then edit .env with your real keys
    uvicorn main:app --reload

This code has NOT been run against live networks in the environment that wrote
it (that sandbox has no internet access) — treat it as a solid, carefully
written starting point, not as pre-verified. Test locally before deploying,
and see README.md for what to check first if something doesn't work.
"""
import hmac
import os
import re
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Optional

import bcrypt
import httpx
import jwt as pyjwt
from fastapi import Depends, FastAPI, Header, HTTPException, Form, Query, Response, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv
from pydantic import BaseModel, EmailStr

try:
    import psycopg2
    import psycopg2.extras
except ImportError:  # psycopg2 not installed locally — Render's build installs it
    psycopg2 = None

load_dotenv()

OCR_SPACE_KEY = os.environ.get("OCR_SPACE_KEY", "helloworld")
GOOGLE_VISION_API_KEY = os.environ.get("GOOGLE_VISION_API_KEY", "")
SPOONACULAR_KEY = os.environ.get("SPOONACULAR_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
DB_PATH = os.environ.get("IL_PRODUCTS_DB", "il_products.sqlite3")
ALLOWED_ORIGINS = os.environ.get("ALLOWED_ORIGINS", "*").split(",")
STORE_ACCESS_KEY = os.environ.get("STORE_ACCESS_KEY", "")
DEV_ACCESS_KEY = os.environ.get("DEV_ACCESS_KEY", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
JWT_SECRET = os.environ.get("JWT_SECRET", "")
JWT_ALGORITHM = "HS256"
JWT_EXPIRY_DAYS = 30

app = FastAPI(title="Hfoody Backend", version="0.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _require_access_key(expected: str, provided: Optional[str]):
    if not expected:
        raise HTTPException(status_code=503, detail="This access tier isn't configured on the server yet")
    if not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="Invalid access key")


async def require_store_key(x_access_key: Optional[str] = Header(None)):
    _require_access_key(STORE_ACCESS_KEY, x_access_key)


async def require_dev_key(x_access_key: Optional[str] = Header(None)):
    _require_access_key(DEV_ACCESS_KEY, x_access_key)


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS store_products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            barcode TEXT,
            name TEXT,
            price REAL,
            ingredients_text TEXT,
            store_name TEXT,
            submitted_at TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_store_barcode ON store_products(barcode)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS shared_kv (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )
    return conn


# ---------------------------------------------------------------------------
# Accounts + cloud profile (Postgres). Mandatory registration for the pilot —
# there is no guest mode. Kept deliberately separate from the SQLite/shared_kv
# store above: cookbook, scan history, shopping list and community recipes
# stay device-local for now and migrate to Postgres in a later phase, once
# accounts themselves are proven working end to end.
# ---------------------------------------------------------------------------

def get_pg_db():
    """Returns a Postgres connection, creating the accounts tables on first
    use. Raises a clear 503 (not a crash) if DATABASE_URL isn't set yet, so
    the rest of the app keeps working even before Postgres is provisioned."""
    if not DATABASE_URL or psycopg2 is None:
        raise HTTPException(
            status_code=503,
            detail="Accounts aren't configured on the server yet (DATABASE_URL missing).",
        )
    conn = psycopg2.connect(DATABASE_URL)
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                name TEXT,
                password_hash TEXT NOT NULL,
                created_at TIMESTAMPTZ DEFAULT now()
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS profiles (
                user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                profile_json TEXT NOT NULL,
                updated_at TIMESTAMPTZ DEFAULT now()
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS feedback (
                id SERIAL PRIMARY KEY,
                user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                message TEXT NOT NULL,
                screen TEXT,
                created_at TIMESTAMPTZ DEFAULT now()
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS user_data (
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                data_key TEXT NOT NULL,
                data_json TEXT NOT NULL,
                updated_at TIMESTAMPTZ DEFAULT now(),
                PRIMARY KEY (user_id, data_key)
            )
            """
        )
    conn.commit()
    return conn


def _hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def _verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False


def _make_token(user_id: int, email: str) -> str:
    if not JWT_SECRET:
        raise HTTPException(status_code=503, detail="Accounts aren't configured on the server yet (JWT_SECRET missing).")
    payload = {
        "sub": str(user_id),
        "email": email,
        "exp": datetime.now(timezone.utc) + timedelta(days=JWT_EXPIRY_DAYS),
    }
    return pyjwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


async def require_auth(authorization: Optional[str] = Header(None)) -> int:
    """Verifies the 'Authorization: Bearer <token>' header and returns the
    user id. Used as a FastAPI dependency on every account-protected route."""
    if not JWT_SECRET:
        raise HTTPException(status_code=503, detail="Accounts aren't configured on the server yet (JWT_SECRET missing).")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or malformed Authorization header")
    token = authorization.removeprefix("Bearer ").strip()
    try:
        payload = pyjwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except pyjwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Session expired — please log in again")
    except pyjwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid session token")
    return int(payload["sub"])


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str
    name: Optional[str] = None


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class ProfileRequest(BaseModel):
    profile: dict


class FeedbackRequest(BaseModel):
    message: str
    screen: Optional[str] = None


@app.post("/api/auth/register")
def register(body: RegisterRequest):
    if len(body.password) < 6:
        raise HTTPException(status_code=400, detail="הסיסמה חייבת להיות באורך 6 תווים לפחות")
    conn = get_pg_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE email = %s", (body.email,))
            if cur.fetchone():
                raise HTTPException(status_code=409, detail="כבר יש חשבון עם האימייל הזה")
            cur.execute(
                "INSERT INTO users (email, name, password_hash) VALUES (%s, %s, %s) RETURNING id",
                (body.email, body.name, _hash_password(body.password)),
            )
            user_id = cur.fetchone()[0]
        conn.commit()
    finally:
        conn.close()
    return {"token": _make_token(user_id, body.email), "user": {"id": user_id, "email": body.email, "name": body.name}}


@app.post("/api/auth/login")
def login(body: LoginRequest):
    conn = get_pg_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name, password_hash FROM users WHERE email = %s", (body.email,))
            row = cur.fetchone()
    finally:
        conn.close()
    if not row or not _verify_password(body.password, row[2]):
        raise HTTPException(status_code=401, detail="אימייל או סיסמה שגויים")
    user_id, name, _ = row
    return {"token": _make_token(user_id, body.email), "user": {"id": user_id, "email": body.email, "name": name}}


@app.get("/api/auth/me")
def auth_me(user_id: int = Depends(require_auth)):
    conn = get_pg_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, email, name FROM users WHERE id = %s", (user_id,))
            row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="User not found")
    return {"id": row[0], "email": row[1], "name": row[2]}


@app.get("/api/profile")
def get_cloud_profile(user_id: int = Depends(require_auth)):
    conn = get_pg_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT profile_json FROM profiles WHERE user_id = %s", (user_id,))
            row = cur.fetchone()
    finally:
        conn.close()
    return {"profile": row[0] if row else None}


@app.post("/api/profile")
def save_cloud_profile(body: ProfileRequest, user_id: int = Depends(require_auth)):
    import json as _json
    conn = get_pg_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO profiles (user_id, profile_json, updated_at) VALUES (%s, %s, now())
                ON CONFLICT (user_id) DO UPDATE SET profile_json = EXCLUDED.profile_json, updated_at = now()
                """,
                (user_id, _json.dumps(body.profile)),
            )
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


@app.post("/api/feedback")
def submit_feedback(body: FeedbackRequest, user_id: int = Depends(require_auth)):
    if not body.message.strip():
        raise HTTPException(status_code=400, detail="Feedback message is empty")
    conn = get_pg_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO feedback (user_id, message, screen) VALUES (%s, %s, %s)",
                (user_id, body.message.strip(), body.screen),
            )
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


# Generic per-user cloud storage — one reusable pair of endpoints instead of
# a separate table per feature. Used for cookbook, scan_history and
# shopping_list (each just a JSON blob keyed by name), the same way
# /api/profile already works for the profile itself.
ALLOWED_USER_DATA_KEYS = {"cookbook", "scan_history", "shopping_list", "weekly_menu"}


class UserDataRequest(BaseModel):
    data: object


@app.get("/api/user-data/{data_key}")
def get_user_data(data_key: str, user_id: int = Depends(require_auth)):
    if data_key not in ALLOWED_USER_DATA_KEYS:
        raise HTTPException(status_code=404, detail="Unknown data key")
    conn = get_pg_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT data_json FROM user_data WHERE user_id = %s AND data_key = %s",
                (user_id, data_key),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    return {"data": row[0] if row else None}


@app.post("/api/user-data/{data_key}")
def save_user_data(data_key: str, body: UserDataRequest, user_id: int = Depends(require_auth)):
    import json as _json
    if data_key not in ALLOWED_USER_DATA_KEYS:
        raise HTTPException(status_code=404, detail="Unknown data key")
    conn = get_pg_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO user_data (user_id, data_key, data_json, updated_at) VALUES (%s, %s, %s, now())
                ON CONFLICT (user_id, data_key) DO UPDATE SET data_json = EXCLUDED.data_json, updated_at = now()
                """,
                (user_id, data_key, _json.dumps(body.data)),
            )
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


@app.get("/api/shared-storage/{key}")
def get_shared_storage(key: str):
    """
    Generic shared key-value store — genuine server-side storage for data that
    must be visible to every user (like community recipes), replacing a
    front-end storage API that only exists inside Claude.ai's own preview
    environment and silently does nothing on a standalone deployed site.
    """
    conn = get_db()
    row = conn.execute("SELECT value FROM shared_kv WHERE key = ?", (key,)).fetchone()
    conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail="Key not found")
    return {"key": key, "value": row[0]}


@app.post("/api/shared-storage/{key}")
async def set_shared_storage(key: str, request: Request):
    body = await request.json()
    conn = get_db()
    conn.execute(
        "INSERT INTO shared_kv (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, body.get("value", "")),
    )
    conn.commit()
    conn.close()
    return {"ok": True}


@app.delete("/api/shared-storage/{key}")
def delete_shared_storage(key: str):
    conn = get_db()
    conn.execute("DELETE FROM shared_kv WHERE key = ?", (key,))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.get("/health")
def health():
    return {"ok": True, "il_products_db_present": os.path.exists(DB_PATH)}


@app.get("/api/capabilities")
def capabilities():
    """
    Public, no-auth endpoint the front-end calls once at startup to know which
    optional features are configured — so a customer never sees a "paste your
    API key" field for anything. Every third-party key lives only here, on
    the server, set once by whoever runs this deployment (Render dashboard or
    the Render MCP connector) — never entered by an end user.
    """
    return {
        "spoonacular": bool(SPOONACULAR_KEY),
        "pantry_search": bool(SPOONACULAR_KEY),
        "vision_ocr": bool(GOOGLE_VISION_API_KEY),
        "ai_analysis": bool(ANTHROPIC_API_KEY),
    }


@app.post("/api/claude")
async def claude_proxy(request: Request):
    """
    Proxies Anthropic's Messages API. This is NOT optional the way Spoonacular
    or Vision OCR are — the app's core feature (health analysis, translation,
    recipe parsing) is entirely unable to work without this, because there is
    no way to call Claude's API from a plain website without a real API key,
    and that key can never live in client-side code. Get one at
    https://console.anthropic.com — note this is pay-per-use, unlike the
    other integrations' free tiers, since it's the actual product.
    """
    if not ANTHROPIC_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="AI analysis isn't configured on the server yet (missing ANTHROPIC_API_KEY)",
        )
    payload = await request.json()
    try:
        async with httpx.AsyncClient(timeout=150) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json=payload,
            )
        return Response(content=resp.content, media_type="application/json", status_code=resp.status_code)
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Claude request failed: {e}")


@app.post("/api/ocr")
async def ocr_proxy(image_base64: str = Form(...), language: str = Form("heb")):
    """
    Recognizes text in `image_base64` (a full data URL, e.g.
    "data:image/jpeg;base64,....", exactly what a <canvas>.toDataURL() or
    FileReader result already gives you client-side).

    Uses Google Cloud Vision when GOOGLE_VISION_API_KEY is set (noticeably
    more accurate on real, messy product labels) and falls back to OCR.space
    otherwise, so nothing on the client needs to know which engine answered.
    """
    if GOOGLE_VISION_API_KEY:
        try:
            return await _ocr_with_google_vision(image_base64)
        except httpx.HTTPError as e:
            raise HTTPException(status_code=502, detail=f"Google Vision request failed: {e}")

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.ocr.space/parse/image",
                data={
                    "apikey": OCR_SPACE_KEY,
                    "base64Image": image_base64,
                    "language": language,
                    "OCREngine": "2",
                    "scale": "true",
                    "isOverlayRequired": "false",
                },
            )
        data = resp.json()
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"OCR.space request failed: {e}")

    if data.get("IsErroredOnProcessing"):
        raise HTTPException(status_code=502, detail=data.get("ErrorMessage"))
    results = data.get("ParsedResults") or []
    text = results[0]["ParsedText"] if results else ""
    return {"text": text.strip(), "engine": "ocr.space"}


async def _ocr_with_google_vision(image_data_url: str):
    # Vision wants raw base64 only — strip the "data:image/...;base64," prefix.
    raw_b64 = re.sub(r"^data:image/[^;]+;base64,", "", image_data_url)
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"https://vision.googleapis.com/v1/images:annotate?key={GOOGLE_VISION_API_KEY}",
            json={
                "requests": [
                    {
                        "image": {"content": raw_b64},
                        "features": [{"type": "DOCUMENT_TEXT_DETECTION"}],
                        # Hebrew label with embedded Latin/numbers is the common case
                        "imageContext": {"languageHints": ["he", "en"]},
                    }
                ]
            },
        )
    data = resp.json()
    resp_obj = (data.get("responses") or [{}])[0]
    if "error" in resp_obj:
        raise HTTPException(status_code=502, detail=resp_obj["error"].get("message", "Google Vision error"))
    text = (resp_obj.get("fullTextAnnotation") or {}).get("text", "")
    return {"text": text.strip(), "engine": "google-vision"}


@app.get("/api/spoonacular/search")
async def spoonacular_search(
    query: Optional[str] = None,
    diet: Optional[str] = None,
    ingredients: Optional[str] = None,
    number: int = Query(8, le=20),
):
    if not SPOONACULAR_KEY:
        raise HTTPException(status_code=400, detail="SPOONACULAR_KEY not configured on the server")
    params = {
        "apiKey": SPOONACULAR_KEY,
        "number": number,
        "addRecipeInformation": "true",
        "fillIngredients": "true",
    }
    if query:
        params["query"] = query
    if diet:
        params["diet"] = diet
    if ingredients:
        params["includeIngredients"] = ingredients
        params["sort"] = "min-missing-ingredients"
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get("https://api.spoonacular.com/recipes/complexSearch", params=params)
        return resp.json()
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Spoonacular request failed: {e}")



@app.get("/api/il-products/search")
def il_products_search(
    barcode: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = Query(20, le=50),
):
    """
    Searches Israeli product data from two sources, store-submitted first
    (more trustworthy — it came from the store itself, including real
    ingredients) then the price-transparency scrape (barcode/name/price only,
    built by refresh_data.py — absent until that's been run at least once).
    """
    if not barcode and not q:
        raise HTTPException(status_code=400, detail="Provide barcode or q")

    conn = get_db()  # ensures store_products exists even on a brand-new DB file
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    results = []

    if barcode:
        cur.execute(
            "SELECT barcode, name, price, ingredients_text, store_name AS chain, "
            "submitted_at AS updated_at, 'store' AS source "
            "FROM store_products WHERE barcode = ? ORDER BY submitted_at DESC LIMIT ?",
            (barcode, limit),
        )
    else:
        cur.execute(
            "SELECT barcode, name, price, ingredients_text, store_name AS chain, "
            "submitted_at AS updated_at, 'store' AS source "
            "FROM store_products WHERE name LIKE ? ORDER BY submitted_at DESC LIMIT ?",
            (f"%{q}%", limit),
        )
    results.extend(dict(r) for r in cur.fetchall())

    try:
        if barcode:
            cur.execute(
                "SELECT barcode, name, price, NULL AS ingredients_text, chain, "
                "updated_at, 'chain' AS source FROM products WHERE barcode = ? LIMIT ?",
                (barcode, limit),
            )
        else:
            cur.execute(
                "SELECT barcode, name, price, NULL AS ingredients_text, chain, "
                "updated_at, 'chain' AS source FROM products WHERE name LIKE ? LIMIT ?",
                (f"%{q}%", limit),
            )
        results.extend(dict(r) for r in cur.fetchall())
    except sqlite3.OperationalError:
        pass  # `products` table doesn't exist yet — refresh_data.py hasn't run, that's fine

    conn.close()
    return {"results": results[:limit]}


@app.post("/api/store/products")
async def submit_store_product(
    barcode: str = Form(...),
    name: str = Form(...),
    price: float = Form(0),
    ingredients_text: str = Form(""),
    store_name: str = Form(""),
    _auth=Depends(require_store_key),
):
    """Access tier B (supermarket): submit a product with real data."""
    conn = get_db()
    conn.execute(
        "INSERT INTO store_products (barcode, name, price, ingredients_text, store_name, submitted_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (barcode.strip(), name.strip(), price, ingredients_text.strip(), store_name.strip(),
         datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()
    return {"ok": True}


@app.get("/api/store/products")
async def list_store_products(limit: int = Query(50, le=200), _auth=Depends(require_store_key)):
    """Lets a logged-in store see what it (or others) has submitted so far."""
    conn = get_db()
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        "SELECT id, barcode, name, price, ingredients_text, store_name, submitted_at "
        "FROM store_products ORDER BY submitted_at DESC LIMIT ?",
        (limit,),
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return {"results": rows}


@app.get("/api/admin/stats")
async def admin_stats(_auth=Depends(require_dev_key)):
    """Access tier C (developer): read-only counts, no PII exposed."""
    conn = get_db()
    counts = {}
    counts["store_products"] = conn.execute("SELECT COUNT(*) FROM store_products").fetchone()[0]
    try:
        counts["chain_products"] = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    except sqlite3.OperationalError:
        counts["chain_products"] = 0
    conn.close()
    return {
        "counts": counts,
        "db_path": DB_PATH,
        "db_size_bytes": os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0,
        "vision_ocr_enabled": bool(GOOGLE_VISION_API_KEY),
        "spoonacular_enabled": bool(SPOONACULAR_KEY),
    }


# Serves the front-end (index.html) on the same origin as the API, so the
# app can call "/api/..." with no CORS setup and no manual backend-URL field.
# Mounted last so it never shadows the /api/* and /health routes above.
# Checks for a frontend/ subfolder first, and falls back to serving straight
# from this file's own folder — so it works whether index.html ended up in
# a frontend/ subfolder or was uploaded alongside main.py at the repo root.
_here = os.path.dirname(__file__)
_frontend_subdir = os.path.join(_here, "frontend")
if os.path.isfile(os.path.join(_frontend_subdir, "index.html")):
    _static_dir = _frontend_subdir
elif os.path.isfile(os.path.join(_here, "index.html")):
    _static_dir = _here
else:
    _static_dir = None

if _static_dir:
    _index_path = os.path.join(_static_dir, "index.html")

    @app.get("/", include_in_schema=False)
    def serve_index():
        # Explicit no-store so phones (mobile Chrome especially) never show a
        # stale cached copy after you push an update — every page load
        # re-fetches the real current file from the server.
        with open(_index_path, "r", encoding="utf-8") as f:
            html = f.read()
        return Response(
            content=html,
            media_type="text/html",
            headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
        )

    # Everything else (icons, manifest.json) can still be cached normally —
    # only the HTML itself needs to always be fresh.
    app.mount("/", StaticFiles(directory=_static_dir), name="frontend")

