"""
Hfoody backend.

Three jobs this service does that a client-only HTML page structurally cannot:
  1. Hold API keys server-side (OCR.space, Spoonacular) instead of shipping them
     to every visitor's browser.
  2. Serve a local, pre-built database of Israeli supermarket products (barcode,
     name, price, chain) — built offline by refresh_data.py from the government
     price-transparency files, since those files can't be fetched directly from
     a browser (no CORS, dozens of inconsistent per-chain formats).
  3. Give you one stable API surface to point the front-end at, regardless of
     which upstream providers you swap in behind it later.

Run locally:
    pip install -r requirements.txt
    cp .env.example .env   # then edit .env with your real keys
    uvicorn main:app --reload

This code has NOT been run against live networks in the environment that wrote
it (that sandbox has no internet access) — treat it as a solid, carefully
written starting point, not as pre-verified. Test locally before deploying,
and see README.md for what to check first if something doesn't work.
"""
import os
import sqlite3
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Form, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv

load_dotenv()

OCR_SPACE_KEY = os.environ.get("OCR_SPACE_KEY", "helloworld")
SPOONACULAR_KEY = os.environ.get("SPOONACULAR_KEY", "")
DB_PATH = os.environ.get("IL_PRODUCTS_DB", "il_products.sqlite3")
ALLOWED_ORIGINS = os.environ.get("ALLOWED_ORIGINS", "*").split(",")

app = FastAPI(title="Hfoody Backend", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {"ok": True, "il_products_db_present": os.path.exists(DB_PATH)}


@app.post("/api/ocr")
async def ocr_proxy(image_base64: str = Form(...), language: str = Form("heb")):
    """
    Proxies OCR.space. `image_base64` is a full data URL
    (e.g. "data:image/jpeg;base64,...."), exactly what a <canvas>.toDataURL()
    or FileReader result already gives you client-side.
    """
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
    return {"text": text.strip()}


@app.get("/api/spoonacular/search")
async def spoonacular_search(
    query: Optional[str] = None,
    diet: Optional[str] = None,
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
    Searches the locally-built Israeli price-transparency product table.
    Populate it first by running refresh_data.py (see README.md) — this
    endpoint returns 503 until that table exists.
    """
    if not os.path.exists(DB_PATH):
        raise HTTPException(
            status_code=503,
            detail="Product database not built yet. Run `python refresh_data.py` first.",
        )
    if not barcode and not q:
        raise HTTPException(status_code=400, detail="Provide barcode or q")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    if barcode:
        cur.execute(
            "SELECT barcode, name, price, chain, store_id, updated_at "
            "FROM products WHERE barcode = ? LIMIT ?",
            (barcode, limit),
        )
    else:
        cur.execute(
            "SELECT barcode, name, price, chain, store_id, updated_at "
            "FROM products WHERE name LIKE ? LIMIT ?",
            (f"%{q}%", limit),
        )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return {"results": rows}


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
    app.mount("/", StaticFiles(directory=_static_dir, html=True), name="frontend")
