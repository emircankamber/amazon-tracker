import os
import json
import sqlite3
import httpx
import asyncio
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
KEEPA_API_KEY          = os.getenv("KEEPA_API_KEY", "")
DB_PATH                = os.getenv("DB_PATH", "data/tracker.db")
CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES", "30"))
GOOGLE_SHEET_ID        = os.getenv("GOOGLE_SHEET_ID", "")
GOOGLE_SERVICE_JSON    = os.getenv("GOOGLE_SERVICE_JSON", "")  # JSON string of service account

KEEPA_DOMAIN = {"amazon.com": 1, "amazon.co.uk": 3, "amazon.de": 3, "amazon.fr": 4}
MARKET_SYMBOL = {"amazon.com": "$", "amazon.co.uk": "£", "amazon.de": "€", "amazon.fr": "€"}

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
                asin       TEXT PRIMARY KEY,
                name       TEXT NOT NULL,
                market     TEXT NOT NULL DEFAULT 'amazon.com',
                is_mine    INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS price_history (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                asin       TEXT NOT NULL,
                price      REAL NOT NULL,
                checked_at TEXT NOT NULL,
                FOREIGN KEY (asin) REFERENCES asins(asin)
            )
        """)
        # Migration: add is_mine if missing
        try:
            conn.execute("ALTER TABLE asins ADD COLUMN is_mine INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass
        conn.commit()

# ---------------------------------------------------------------------------
# Keepa
# ---------------------------------------------------------------------------
async def fetch_keepa_price(asin: str, market: str) -> float | None:
    if not KEEPA_API_KEY:
        return None
    domain_id = KEEPA_DOMAIN.get(market, 1)
    url = f"https://api.keepa.com/product?key={KEEPA_API_KEY}&domain={domain_id}&asin={asin}&stats=1"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(url)
            r.raise_for_status()
            data = r.json()
        products = data.get("products", [])
        if not products:
            return None
        csv = products[0].get("csv", [])
        if not csv or len(csv) < 2 or not csv[1] or len(csv[1]) < 2:
            return None
        raw = csv[1][-1]
        return round(raw / 100, 2) if raw > 0 else None
    except Exception as e:
        print(f"[Keepa] {asin} hata: {e}")
        return None

# ---------------------------------------------------------------------------
# Trend score  (-100 to +100)
# ---------------------------------------------------------------------------
def calc_trend(prices: list[float]) -> int:
    if len(prices) < 2:
        return 0
    changes = [(prices[i] - prices[i-1]) / prices[i-1] * 100 for i in range(1, len(prices))]
    avg = sum(changes) / len(changes)
    return max(-100, min(100, int(avg * 10)))

# ---------------------------------------------------------------------------
# Google Sheets sync
# ---------------------------------------------------------------------------
async def sync_to_sheets(rows: list[dict]):
    if not GOOGLE_SHEET_ID or not GOOGLE_SERVICE_JSON:
        return
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        creds_dict = json.loads(GOOGLE_SERVICE_JSON)
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(GOOGLE_SHEET_ID)
        ws = sh.sheet1
        header = ["ASIN", "Ürün", "Pazar", "Benim?", "Şu An", "Önceki", "Değişim%", "Trend Skoru", "Son Güncelleme"]
        data = [header]
        for r in rows:
            diff_pct = ""
            if r.get("price") and r.get("prev") and r["prev"]:
                diff_pct = f"{((r['price']-r['prev'])/r['prev']*100):.1f}%"
            data.append([
                r["asin"], r["name"], r["market"],
                "Evet" if r.get("is_mine") else "Hayır",
                r.get("price") or "", r.get("prev") or "",
                diff_pct, r.get("trend", 0),
                r.get("updated") or ""
            ])
        ws.clear()
        ws.update("A1", data)
        print(f"[Sheets] {len(rows)} satır senkronize edildi")
    except Exception as e:
        print(f"[Sheets] Hata: {e}")

# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------
async def check_all_prices():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Fiyat kontrol başladı")
    with get_db() as conn:
        rows = conn.execute("SELECT asin, market FROM asins").fetchall()
    all_data = []
    for row in rows:
        price = await fetch_keepa_price(row["asin"], row["market"])
        if price is not None:
            with get_db() as conn:
                conn.execute(
                    "INSERT INTO price_history (asin, price, checked_at) VALUES (?,?,?)",
                    (row["asin"], price, datetime.now(timezone.utc).isoformat())
                )
                conn.commit()
        await asyncio.sleep(0.5)
    # Google Sheets sync after check
    full = _build_asin_list()
    await sync_to_sheets(full)
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Kontrol bitti")

def _build_asin_list() -> list[dict]:
    with get_db() as conn:
        asins = conn.execute("SELECT * FROM asins").fetchall()
        result = []
        for a in asins:
            history = conn.execute(
                "SELECT price, checked_at FROM price_history WHERE asin=? ORDER BY checked_at DESC LIMIT 30",
                (a["asin"],)
            ).fetchall()
            prices = [h["price"] for h in history]
            times  = [h["checked_at"] for h in history]
            current = prices[0] if prices else None
            prev    = prices[1] if len(prices) > 1 else current
            trend   = calc_trend(list(reversed(prices[:14]))) if len(prices) >= 2 else 0
            result.append({
                "asin":    a["asin"],
                "name":    a["name"],
                "market":  a["market"],
                "symbol":  MARKET_SYMBOL.get(a["market"], "$"),
                "is_mine": bool(a["is_mine"]),
                "price":   current,
                "prev":    prev,
                "history": list(reversed(prices[:14])),
                "times":   list(reversed(times[:14])),
                "updated": times[0] if times else None,
                "trend":   trend,
            })
    return result

# ---------------------------------------------------------------------------
# App
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
# Models
# ---------------------------------------------------------------------------
class ASINCreate(BaseModel):
    asin: str
    name: str
    market: str = "amazon.com"
    is_mine: bool = False

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/api/asins")
def list_asins():
    return _build_asin_list()

@app.post("/api/asins")
async def add_asin(body: ASINCreate):
    asin = body.asin.strip().upper()
    if len(asin) != 10:
        raise HTTPException(400, "ASIN 10 karakter olmalı")
    with get_db() as conn:
        if conn.execute("SELECT asin FROM asins WHERE asin=?", (asin,)).fetchone():
            raise HTTPException(409, "Bu ASIN zaten listede")
        conn.execute(
            "INSERT INTO asins (asin, name, market, is_mine, created_at) VALUES (?,?,?,?,?)",
            (asin, body.name or ("Benim Ürünüm" if body.is_mine else "Rakip Ürün"),
             body.market, int(body.is_mine), datetime.now(timezone.utc).isoformat())
        )
        conn.commit()
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
        "sheets_configured": bool(GOOGLE_SHEET_ID and GOOGLE_SERVICE_JSON),
    }

@app.get("/api/history/{asin}")
def get_history(asin: str):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT price, checked_at FROM price_history WHERE asin=? ORDER BY checked_at ASC",
            (asin,)
        ).fetchall()
    return [{"price": r["price"], "time": r["checked_at"]} for r in rows]

@app.get("/api/compare")
def compare():
    """Returns all ASINs with full history for comparison chart"""
    with get_db() as conn:
        asins = conn.execute("SELECT asin, name, is_mine FROM asins").fetchall()
        result = []
        for a in asins:
            rows = conn.execute(
                "SELECT price, checked_at FROM price_history WHERE asin=? ORDER BY checked_at ASC LIMIT 50",
                (a["asin"],)
            ).fetchall()
            result.append({
                "asin": a["asin"],
                "name": a["name"],
                "is_mine": bool(a["is_mine"]),
                "data": [{"price": r["price"], "time": r["checked_at"]} for r in rows]
            })
    return result

@app.get("/api/export-csv")
def export_csv():
    import io, csv
    with get_db() as conn:
        rows = conn.execute("""
            SELECT a.asin, a.name, a.market, a.is_mine, ph.price, ph.checked_at
            FROM price_history ph
            JOIN asins a ON a.asin = ph.asin
            ORDER BY ph.checked_at DESC
        """).fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["ASIN", "Ürün Adı", "Pazar", "Benim?", "Fiyat", "Kontrol Zamanı"])
    for r in rows:
        w.writerow([r["asin"], r["name"], r["market"], "Evet" if r["is_mine"] else "Hayır", r["price"], r["checked_at"]])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=fiyat_gecmisi.csv"}
    )

app.mount("/", StaticFiles(directory="static", html=True), name="static")
