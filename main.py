import os
import json
import sqlite3
import httpx
import asyncio
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
KEEPA_API_KEY = os.getenv("KEEPA_API_KEY", "")
DB_PATH = os.getenv("DB_PATH", "data/tracker.db")
CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES", "30"))

KEEPA_DOMAIN = {
    "amazon.com": 1,
    "amazon.co.uk": 3,
    "amazon.de": 3,
    "amazon.fr": 4,
}

MARKET_SYMBOL = {
    "amazon.com": "$",
    "amazon.co.uk": "£",
    "amazon.de": "€",
    "amazon.fr": "€",
}

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
def get_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS asins (
                asin        TEXT PRIMARY KEY,
                name        TEXT NOT NULL,
                market      TEXT NOT NULL DEFAULT 'amazon.com',
                created_at  TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS price_history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                asin        TEXT NOT NULL,
                price       REAL NOT NULL,
                checked_at  TEXT NOT NULL,
                FOREIGN KEY (asin) REFERENCES asins(asin)
            )
        """)
        conn.commit()

# ---------------------------------------------------------------------------
# Keepa fetch
# ---------------------------------------------------------------------------
async def fetch_keepa_price(asin: str, market: str) -> float | None:
    if not KEEPA_API_KEY:
        return None
    domain_id = KEEPA_DOMAIN.get(market, 1)
    url = (
        f"https://api.keepa.com/product"
        f"?key={KEEPA_API_KEY}&domain={domain_id}&asin={asin}&stats=1"
    )
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(url)
            r.raise_for_status()
            data = r.json()
        products = data.get("products", [])
        if not products:
            return None
        csv = products[0].get("csv", [])
        # csv[1] = Amazon price history, alternating [keepa_time, price, ...]
        if not csv or len(csv) < 2 or not csv[1] or len(csv[1]) < 2:
            return None
        raw = csv[1][-1]          # last value
        return round(raw / 100, 2) if raw > 0 else None
    except Exception as e:
        print(f"[Keepa] {asin} hata: {e}")
        return None

# ---------------------------------------------------------------------------
# Scheduler job
# ---------------------------------------------------------------------------
async def check_all_prices():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Fiyat kontrol başladı")
    with get_db() as conn:
        rows = conn.execute("SELECT asin, market FROM asins").fetchall()
    for row in rows:
        price = await fetch_keepa_price(row["asin"], row["market"])
        if price is not None:
            with get_db() as conn:
                conn.execute(
                    "INSERT INTO price_history (asin, price, checked_at) VALUES (?,?,?)",
                    (row["asin"], price, datetime.now(timezone.utc).isoformat())
                )
                conn.commit()
            print(f"  {row['asin']} → {price}")
        await asyncio.sleep(0.5)   # Keepa rate limit
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Kontrol bitti")

# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------
scheduler = AsyncIOScheduler()

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    scheduler.add_job(check_all_prices, "interval", minutes=CHECK_INTERVAL_MINUTES, id="price_check")
    scheduler.start()
    print(f"Scheduler başlatıldı — her {CHECK_INTERVAL_MINUTES} dakikada kontrol")
    yield
    scheduler.shutdown()

app = FastAPI(title="Amazon Rakip Fiyat Takip", lifespan=lifespan)

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------
class ASINCreate(BaseModel):
    asin: str
    name: str
    market: str = "amazon.com"

# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------
@app.get("/api/asins")
def list_asins():
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM asins").fetchall()
        result = []
        for row in rows:
            asin = row["asin"]
            history = conn.execute(
                "SELECT price, checked_at FROM price_history WHERE asin=? ORDER BY checked_at DESC LIMIT 30",
                (asin,)
            ).fetchall()
            prices = [h["price"] for h in history]
            times  = [h["checked_at"] for h in history]
            current = prices[0] if prices else None
            prev    = prices[1] if len(prices) > 1 else current
            result.append({
                "asin":    asin,
                "name":    row["name"],
                "market":  row["market"],
                "symbol":  MARKET_SYMBOL.get(row["market"], "$"),
                "price":   current,
                "prev":    prev,
                "history": list(reversed(prices[:14])),
                "times":   list(reversed(times[:14])),
                "updated": times[0] if times else None,
            })
    return result

@app.post("/api/asins")
async def add_asin(body: ASINCreate):
    asin = body.asin.strip().upper()
    if len(asin) != 10:
        raise HTTPException(400, "ASIN 10 karakter olmalı")
    with get_db() as conn:
        existing = conn.execute("SELECT asin FROM asins WHERE asin=?", (asin,)).fetchone()
        if existing:
            raise HTTPException(409, "Bu ASIN zaten listede")
        conn.execute(
            "INSERT INTO asins (asin, name, market, created_at) VALUES (?,?,?,?)",
            (asin, body.name or "Rakip Ürün", body.market, datetime.now(timezone.utc).isoformat())
        )
        conn.commit()
    # İlk fiyatı hemen çek
    price = await fetch_keepa_price(asin, body.market)
    if price:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO price_history (asin, price, checked_at) VALUES (?,?,?)",
                (asin, price, datetime.now(timezone.utc).isoformat())
            )
            conn.commit()
    return {"ok": True, "asin": asin, "price": price}

@app.delete("/api/asins/{asin}")
def delete_asin(asin: str):
    with get_db() as conn:
        conn.execute("DELETE FROM price_history WHERE asin=?", (asin,))
        conn.execute("DELETE FROM asins WHERE asin=?", (asin,))
        conn.commit()
    return {"ok": True}

@app.post("/api/check-now")
async def check_now():
    await check_all_prices()
    return {"ok": True, "checked_at": datetime.now(timezone.utc).isoformat()}

@app.get("/api/status")
def status():
    with get_db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM asins").fetchone()[0]
        last  = conn.execute("SELECT MAX(checked_at) FROM price_history").fetchone()[0]
    return {
        "total_asins": total,
        "last_check": last,
        "interval_minutes": CHECK_INTERVAL_MINUTES,
        "keepa_configured": bool(KEEPA_API_KEY),
    }

@app.get("/api/history/{asin}")
def get_history(asin: str):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT price, checked_at FROM price_history WHERE asin=? ORDER BY checked_at ASC",
            (asin,)
        ).fetchall()
    return [{"price": r["price"], "time": r["checked_at"]} for r in rows]

@app.get("/api/export-csv")
def export_csv():
    from fastapi.responses import StreamingResponse
    import io, csv
    with get_db() as conn:
        rows = conn.execute("""
            SELECT a.asin, a.name, a.market,
                   ph.price, ph.checked_at
            FROM price_history ph
            JOIN asins a ON a.asin = ph.asin
            ORDER BY ph.checked_at DESC
        """).fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["ASIN", "Ürün Adı", "Pazar", "Fiyat", "Kontrol Zamanı"])
    for r in rows:
        w.writerow([r["asin"], r["name"], r["market"], r["price"], r["checked_at"]])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=fiyat_gecmisi.csv"}
    )

# Static files (frontend)
app.mount("/", StaticFiles(directory="static", html=True), name="static")
