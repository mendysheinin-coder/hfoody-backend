import hmac
import os
import re
import sqlite3
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Form, Query, Response, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv

load_dotenv()

OCR_SPACE_KEY = os.environ.get("OCR_SPACE_KEY", "helloworld")
GOOGLE_VISION_API_KEY = os.environ.get("GOOGLE_VISION_API_KEY", "")
SPOONACULAR_KEY = os.environ.get("SPOONACULAR_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
DB_PATH = os.environ.get("IL_PRODUCTS_DB", "il_products.sqlite3")
ALLOWED_ORIGINS = os.environ.get("ALLOWED_ORIGINS", "*").split(",")
STORE_ACCESS_KEY = os.environ.get("STORE_ACCESS_KEY", "")
DEV_ACCESS_KEY = os.environ.get("DEV_ACCESS_KEY", "")

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
    return conn


@app.get("/health")
def health():
    return {"ok": True, "il_products_db_present": os.path.exists(DB_PATH)}


@app.get("/api/capabilities")
def capabilities():
    return {
        "spoonacular": bool(SPOONACULAR_KEY),
        "pantry_search": bool(SPOONACULAR_KEY),
        "vision_ocr": bool(GOOGLE_VISION_API_KEY),
        "ai_analysis": bool(ANTHROPIC_API_KEY),
    }


@app.post("/api/claude")
async def claude_proxy(request: Request):
    if not ANTHROPIC_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="AI analysis isn't configured on the server yet (missing ANTHROPIC_API_KEY)",
        )
    payload = await request.json()
    try:
        async with httpx.AsyncClient(timeout=45) as client:
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
    raw_b64 = re.sub(r"^data:image/[^;]+;base64,", "", image_data_url)
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"https://vision.googleapis.com/v1/images:annotate?key={GOOGLE_VISION_API_KEY}",
            json={
                "requests": [
                    {
                        "image": {"content": raw_b64},
                        "features": [{"type": "DOCUMENT_TEXT_DETECTION"}],
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
    if not barcode and not q:
        raise HTTPException(status_code=400, detail="Provide barcode or q")

    conn = get_db()
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
        pass

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
        with open(_index_path, "r", encoding="utf-8") as f:
            html = f.read()
        return Response(
            content=html,
            media_type="text/html",
            headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
        )

    app.mount("/", StaticFiles(directory=_static_dir), name="frontend")
