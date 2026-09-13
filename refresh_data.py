"""
Builds/refreshes il_products.sqlite3 from Israel's supermarket price-transparency
files, using the community-maintained OpenIsraeliSupermarkets packages:
  - il-supermarket-scraper  (downloads each chain's published price/store files)
  - il-supermarket-parser   (normalizes the mess of per-chain XML into flat tables)
  https://github.com/OpenIsraeliSupermarkets

Run this on a schedule — a cron job, a Render/Railway "cron job" service, or a
GitHub Action that pushes the resulting .sqlite3 file somewhere the API can read
it. The source files are refreshed by each chain on their own portal (often
hourly), so a stale local DB just means stale prices, not broken behavior.

IMPORTANT — this script has not been executed against the live network in the
environment that wrote it (no internet access there). Before you rely on it:
  1. `pip install -r requirements.txt`
  2. In a Python shell: `from il_supermarket_scarper import ScraperFactory;
     print(list(ScraperFactory))` — confirm the exact enum names below match
     what your installed version actually exposes (library APIs shift).
  3. Run this script with ENABLED_SCRAPERS trimmed to ONE chain first, check
     the output CSVs land where load_into_sqlite() expects, then expand.
  4. The parser's output column names (ItemCode/ItemName/ItemPrice, etc.) are
     also worth double-checking against a real output file — adjust the
     `cols.get(...)` lines in load_into_sqlite() to match if they differ.
"""
import glob
import os
import sqlite3

import pandas as pd
from il_supermarket_parser import ConvertingTask
from il_supermarket_scarper import ScarpingTask, ScraperFactory

DUMP_DIR = "dumps"
OUTPUT_DIR = "outputs"
DB_PATH = os.environ.get("IL_PRODUCTS_DB", "il_products.sqlite3")

# Start small and confirmed-working, then add more chains from ScraperFactory.
# Some chains are geo-blocked outside Israel, or occasionally flaky — the
# project's own test suite (README badge) tracks which ones currently work.
ENABLED_SCRAPERS = [
    ScraperFactory.SHUFERSAL.name,
]


def scrape():
    task = ScarpingTask(
        enabled_scrapers=ENABLED_SCRAPERS,
        dump_folder_name=DUMP_DIR,
        limit=None,
    )
    task.start()


def parse():
    task = ConvertingTask(data_folder=DUMP_DIR, output_folder=OUTPUT_DIR)
    task.run()


def load_into_sqlite():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS products (
            barcode TEXT,
            name TEXT,
            price REAL,
            chain TEXT,
            store_id TEXT,
            updated_at TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_barcode ON products(barcode)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_name ON products(name)")
    conn.execute("DELETE FROM products")  # simple full-refresh strategy

    csv_files = glob.glob(os.path.join(OUTPUT_DIR, "**", "*.csv"), recursive=True)
    if not csv_files:
        print(f"No CSV files found under {OUTPUT_DIR}/ — did scrape()/parse() run first?")

    inserted = 0
    for csv_path in csv_files:
        try:
            df = pd.read_csv(csv_path)
        except Exception as e:
            print(f"skipping {csv_path}: {e}")
            continue

        cols = {c.lower(): c for c in df.columns}
        barcode_col = cols.get("itemcode") or cols.get("barcode")
        name_col = cols.get("itemname") or cols.get("name")
        price_col = cols.get("itemprice") or cols.get("price")
        store_col = cols.get("storeid") or cols.get("store_id")
        if not (barcode_col and name_col and price_col):
            print(f"skipping {csv_path}: couldn't find barcode/name/price columns "
                  f"(has: {list(df.columns)})")
            continue

        chain = os.path.basename(csv_path).split("_")[0]
        for _, row in df.iterrows():
            try:
                price = float(row.get(price_col, 0) or 0)
            except (TypeError, ValueError):
                price = 0.0
            conn.execute(
                "INSERT INTO products (barcode, name, price, chain, store_id, updated_at) "
                "VALUES (?, ?, ?, ?, ?, datetime('now'))",
                (
                    str(row.get(barcode_col, "")),
                    str(row.get(name_col, "")),
                    price,
                    chain,
                    str(row.get(store_col, "")) if store_col else "",
                ),
            )
            inserted += 1

    conn.commit()
    conn.close()
    print(f"Loaded {inserted} product rows into {DB_PATH}")


if __name__ == "__main__":
    print("Scraping...")
    scrape()
    print("Parsing...")
    parse()
    print("Loading into SQLite...")
    load_into_sqlite()
    print("Done. Start the API and query /api/il-products/search")
