# FinPlus+ Stock Scanner (APP-02) — Deploy & Monday Readiness Checklist (v2.7.2)

This one-page operational checklist covers production deployment verification on Render, Firebase persistence validation, history coverage restore, and Monday morning market opening checks.

---

## 1. Environment & Secrets Setup (Render Dashboard)

Ensure the following environment variables are configured in the Render service:
- `FIREBASE_CREDENTIALS_JSON`: Full raw JSON string of the Firebase service account key.
- `FIREBASE_ENV_PREFIX`: `live`
- `ENABLE_LEGACY_YAHOO_SCAN_PATH`: `true` *(Keep ON as safety path until 2 sessions of bar parity pass)*
- `NTFY_TOPIC`: Configured NTFY alert topic (e.g. `Finplus-Delta`).
- `NTFY_AUTH_TOKEN`: Optional NTFY Bearer token. If rejected (401/403), system logs ERROR (never logs token) and reports `notifications: TOKEN_REJECTED` in `/api/health/deep`.
- `INDMONEY_CLIENT_ID`, `INDMONEY_TOTP_SECRET`, `INDMONEY_MPIN`: Active INDstocks broker credentials.
- `VIRTUAL_CAPITAL`: Default `100000.0`.

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
    - `data_gz_b64`: Compressed gzipped candle matrix string (~3-5 KB each).
- **Other Collections Mirrored Automatically**:
  - `env/live/paper_trades`: Active and closed paper trades with trailing stop states and Taken flags.
  - `env/live/latches`: Daily deduplication latches.
  - `env/live/state`: Virtual capital and NTFY quota counters.
  - `env/live/schedule_ledger`: Executed milestone records.

---

## 3. URLs & Expected Verification Values

After deployment starts on Render, check the following endpoints:

| Endpoint | Expected Status | Key Value to Verify |
| :--- | :--- | :--- |
| `GET /api/health` | HTTP 200 | `{"status": "ok", "app": "FinPlus-Stock-Scanner"}` |
| `GET /api/health/deep` | HTTP 200 (Off-Hours) | `"firebase": {"status": "connected", "is_connected": true}` |
| | | `"history_coverage.coverage_522_pct"`: `>= 90.0%` |
| | | `"history_coverage.last_restore_reason"`: Shows why restore ran or skipped |
| | | `"code_version"`: Git short commit hash and branch (`release/v2.7.2`) |
| | | `"subsystems.notifications"`: `"HEALTHY"` (or `"TOKEN_REJECTED"` if invalid token) |
| `GET /api/yfinance/usage`| HTTP 200 | `"strict_calls_used": 0` (0 Yahoo calls made for data in Firestore) |
| `GET /api/bar_parity` | HTTP 200 | `"sessions_evaluated"`, parity %, ready status flag |
| `GET /api/paper_trades` | HTTP 200 | Paper trades, Top 12 ranked by default with "Show All" toggle |

---

## 4. Startup & Schedule Sequence

1. **Cold Startup & Restore Rule (< 90% Coverage)**:
   - At startup, the server calculates history coverage across tracked universe.
   - If coverage < 90% (< 90% of symbols have >= 522 bars), it restores candle history from Firestore **BEFORE** the scanner starts.
   - If Firestore brings coverage >= 90%, zero Yahoo calls are made.
   - Paced Yahoo backfill only downloads symbols still missing after Firestore restore.
   - Dispatches one priority startup NTFY alert: `"[STARTUP] version <hash> online | Firebase connected | history X% | token OK"`.
2. **08:30 IST Scheduled Check**:
   - Re-checks history coverage and restores missing symbols from Firestore before pre-market.
3. **Weekends & Holidays Ledger Rule**:
   - On Saturdays, Sundays, and NSE holidays, all 09:00 to 16:00 bulletins are marked `SKIPPED_MARKET_CLOSED` at start of day. None are left `PENDING` or fired.
   - On Sundays at 18:00 IST, `18:00_WEEKLY_REPORT` fires the weekly recap.
4. **09:15 – 09:30 IST (First 15-Minute Candle Formation)**:
   - INDmoney feeds live ticks into `CandleStore`.
   - Forming bar wicks are captured from day high / low.
   - First 15m candle closes at 09:30 IST.
5. **09:30 IST (First Scan Cycle)**:
   - Range detector scans universe using `CandleStore` + local cache.
   - Zero strict yfinance calls are made during normal scans.
6. **15:45 IST (Post-Market Sync)**:
   - `upload_all_candles_from_sqlite()` runs automatically, uploading all newly formed bars to `env/live/candles` in Firestore.

---

## 5. What is NOT a Problem (Expected Behaviors)

- **`"market_open": false` outside 09:15 - 15:30 IST**: Normal off-hours behavior; health returns `HEALTHY_OFF_HOURS` (HTTP 200).
- **`SKIPPED_MARKET_CLOSED` on Weekends/Holidays**: All market bulletins are marked skipped with reason `Market closed (Weekend)` or `Market closed (NSE Holiday: ...)`.
- **`yfinance Legacy Scan Calls > 0`**: The legacy Yahoo path is intentionally ON (`ENABLE_LEGACY_YAHOO_SCAN_PATH=true`) as a safety valve. It has a separate 3,000 call/day ceiling and does not deplete the strict budget.
- **Render Ephemeral Disk Warning**: The warning `"PERSISTENCE_NOTICE: Local files are temporary on Render"` disappears once `FIREBASE_CREDENTIALS_JSON` is active and connected.
- **Render Free 512 MB Memory**: App runs efficiently under ~160 MB RSS through streaming SQLite inserts and lazy loading.

