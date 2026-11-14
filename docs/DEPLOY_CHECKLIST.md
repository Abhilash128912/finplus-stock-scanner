# FinPlus+ Stock Scanner (APP-02) — Deploy & Monday Readiness Checklist

This one-page operational checklist covers production deployment verification on Render, Firebase persistence validation, and Monday morning market opening checks.

---

## 1. Environment & Secrets Setup (Render Dashboard)

Ensure the following environment variables are configured in the Render service:
- `FIREBASE_CREDENTIALS_JSON`: Full raw JSON string of the Firebase service account key.
- `FIREBASE_ENV_PREFIX`: `live`
- `ENABLE_LEGACY_YAHOO_SCAN_PATH`: `true` *(Keep ON as safety path until 2 sessions of bar parity pass)*
- `NTFY_TOPIC`: Configured NTFY alert topic.
- `INDMONEY_TOTP_SECRET` & credentials: Active INDstocks credentials.

---

## 2. Firebase Console Verification

Open [Google Cloud / Firebase Console](https://console.firebase.google.com/):
- **Database**: Cloud Firestore in `(default)` database.
- **Collection**: `env/live/candles`
  - Expected documents: **785 documents** (one document per symbol, e.g., `RELIANCE_15m`, `TCS_15m`).
  - Document fields:
    - `symbol`: e.g. `"RELIANCE"`
    - `timeframe`: `"15m"`
    - `bars_count`: ~700-750 bars
    - `data_gz_b64`: Compressed gzipped candle matrix string (~9-10 KB each).
- **Other Collections Created Automatically During Trading**:
  - `env/live/paper_trades`: Active and closed paper trades.
  - `env/live/latches`: Daily deduplication latches.
  - `env/live/state`: Virtual capital and NTFY quota counters.
  - `env/live/schedule_ledger`: Executed milestone records.

---

## 3. URLs & Expected Verification Values

After deployment starts on Render, check the following endpoints:

| Endpoint | Expected Status | Key Value to Verify |
| :--- | :--- | :--- |
| `GET /api/health` | HTTP 200 | `{"status": "ok", "app": "FinPlus-Stock-Scanner"}` |
| `GET /api/health/deep` | HTTP 200 (Off-Hours) | `"firebase": {"status": "CONNECTED (live)", "is_connected": true}` |
| | | `"firebase.last_restore.status"`: `"SUCCESS"` (on cold boot) |
| | | `"subsystems.broker"`: `"HEALTHY"` |
| `GET /api/yfinance/usage`| HTTP 200 | `"strict_calls_used": 0` (market hours with healthy cache) |
| `GET /api/bar_parity` | HTTP 200 | `"sessions_evaluated"`, parity %, ready status flag |
| `GET /api/paper/positions` | HTTP 200 | Open and closed paper trade positions |

---

## 4. Monday Morning Market Opening Sequence (09:00 – 09:30 IST)

1. **09:00 – 09:15 IST (Pre-Market Warmup)**:
   - Server runs scheduled pre-market checks.
   - `/api/health/deep` transitions to `"market_open": true` at 09:15 IST.
2. **09:15 – 09:30 IST (First 15-Minute Candle Formation)**:
   - INDmoney feeds live ticks into `CandleStore`.
   - Forming bar wicks are captured from day high / low.
   - First 15m candle closes at 09:30 IST.
3. **09:30 IST (First Scan Cycle)**:
   - Range detector scans 858 symbols using `CandleStore` + local cache.
   - Zero strict yfinance calls are made during normal scans.
4. **15:45 IST (Post-Market Sync)**:
   - `upload_all_candles_from_sqlite()` runs automatically, uploading all newly formed bars to `env/live/candles` in Firestore.

---

## 5. What is NOT a Problem (Expected Behaviors)

- **`"market_open": false` outside 09:15 - 15:30 IST**: This is normal off-hours behavior; health returns `HEALTHY_OFF_HOURS` (HTTP 200).
- **`yfinance Legacy Scan Calls > 0`**: The legacy Yahoo path is intentionally ON (`ENABLE_LEGACY_YAHOO_SCAN_PATH=true`) as a safety valve. It has a separate 3,000 call/day ceiling and does not deplete the strict budget.
- **`LOCAL_ONLY` on local laptop**: If `FIREBASE_CREDENTIALS_JSON` is not set locally on your machine, it safely falls back to local SQLite and will not overwrite production.
- **Render Ephemeral Disk Warning**: The warning `"PERSISTENCE_NOTICE: Local files are temporary on Render"` disappears once `FIREBASE_CREDENTIALS_JSON` is active and connected.

