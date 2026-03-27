# Amazon Rakip Fiyat Takip Paneli

Keepa API ile rakip ASIN fiyatlarını otomatik takip eder.

---

## Kurulum (Railway — ücretsiz)

### 1. Adım — GitHub'a yükle

```bash
# Bu klasörde terminal aç
git init
git add .
git commit -m "ilk commit"
```

GitHub'da yeni repo oluştur → `amazon-tracker` adında, **private** yap.

```bash
git remote add origin https://github.com/KULLANICI_ADIN/amazon-tracker.git
git push -u origin main
```

---

### 2. Adım — Railway hesabı aç

1. https://railway.app → **Login with GitHub** ile giriş yap
2. **New Project** → **Deploy from GitHub repo** → `amazon-tracker` seç
3. Railway otomatik algılar ve deploy eder (~2 dk)

---

### 3. Adım — Environment Variables (çok önemli)

Railway dashboard → proje → **Variables** sekmesi:

| Değişken | Değer | Açıklama |
|---|---|---|
| `KEEPA_API_KEY` | `abc123...` | keepa.com'dan al |
| `CHECK_INTERVAL_MINUTES` | `30` | kaç dakikada bir kontrol |
| `DB_PATH` | `data/tracker.db` | değiştirme |

> **Keepa API key almak:** keepa.com → hesap oluştur → API Access → key kopyala  
> Ücretsiz plan: 50 token/dakika (her ASIN sorgusu 1-2 token)

---

### 4. Adım — Domain al

Railway dashboard → **Settings** → **Domains** → **Generate Domain**

`https://amazon-tracker-production.up.railway.app` gibi bir URL alırsın.

---

## Yerel test (opsiyonel)

```bash
pip install -r requirements.txt

# .env dosyası oluştur
echo "KEEPA_API_KEY=senin_key" > .env
echo "CHECK_INTERVAL_MINUTES=30" >> .env

# Çalıştır
uvicorn main:app --reload
```

Tarayıcıda: http://localhost:8000

---

## API Endpoints

| Endpoint | Metod | Açıklama |
|---|---|---|
| `/api/asins` | GET | Tüm ASIN listesi + son fiyatlar |
| `/api/asins` | POST | Yeni ASIN ekle |
| `/api/asins/{asin}` | DELETE | ASIN sil |
| `/api/check-now` | POST | Anında tüm fiyatları kontrol et |
| `/api/history/{asin}` | GET | ASIN geçmiş fiyatları |
| `/api/export-csv` | GET | Tüm geçmişi CSV indir |
| `/api/status` | GET | Servis durumu |

---

## Notlar

- Fiyat geçmişi SQLite'ta kalır, Railway yeniden başlasa bile silinmez (volume bağlarsan)
- Railway ücretsiz planda aylık 500 saat çalışır — tek proje için yeterli
- Keepa token harcaması: 30 dakikada bir × ASIN sayısı. 20 ASIN = 1440 token/gün
