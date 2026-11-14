# FinPlus+ Stock Scanner (APP-02) — Deploy & Monday Readiness Checklist (v2.7.3)

This one-page operational checklist covers production deployment verification on Render, Firebase persistence validation, history coverage restore, Preflight diagnostics, and Monday morning market opening checks.

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
- **NEW (Safety Switches & Auth in v2.7.3)**:
  - `ADMIN_TOKEN`: Bearer secret protecting `POST /api/admin/restore-now` (401 without Bearer, 403 if mismatch).
  - `PANEL_USER`: HTTP Basic Auth username for trading panel and mutating operations.
  - `PANEL_PASSWORD`: HTTP Basic Auth password for trading panel.
  - `ALERTS_ENABLED`: `true` (default). When set to `false`, all outgoing NTFY pushes are suppressed while paper trading, web UI, and Firebase persistence continue normally.

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
| `GET /api/health` | HTTP 200 (Public) | `{"status": "ok", "app": "FinPlus-Stock-Scanner"}` |
| `GET /preflight` | HTTP 200 (Public) | Clean HTML Dashboard with GREEN / AMBER / RED cards |
| `GET /api/preflight` | HTTP 200 (Public) | JSON summary: `status` ("GREEN"/"AMBER"/"RED"), `all_green: true` |
| `GET /api/health/deep` | HTTP 200 (Public) | `"firebase.status"`: `"connected"`, `"history_coverage.coverage_522_pct"`: `>= 90.0%`, `"code_version.commit"`: Short hash |
| `POST /api/admin/restore-now` | HTTP 200 (Bearer ADMIN_TOKEN) | `{"status": "SUCCESS", "symbols_restored": N, "yfinance_calls": 0}` |
| `GET /api/yfinance/usage`| HTTP 200 | `"strict_calls_used": 0` (0 Yahoo calls made for data in Firestore) |
| `GET /api/bar_parity` | HTTP 200 | `"sessions_evaluated"`, parity %, ready status flag |
| `GET /api/paper_trades` | HTTP 200 (or Basic Auth) | Paper trades, Top 12 ranked by default with "Show All" toggle |

---

## 4. Preflight Diagnostics & Meaning of RED Items

The Preflight system runs automatically and displays health on `/preflight` and `/api/preflight`. At **08:45 IST** on trading days, it sends ONE priority NTFY bulletin: `"[PREFLIGHT] ALL GREEN"` or lists any RED items.

### Meaning of RED Items:
1. **`fno_coverage` (RED)**: Qualifying liquid F&O stocks (< 500 lot + RELIANCE) is under 90 instruments. Check NSE `fo_mktlots.csv` fetch.
2. **`history_coverage` (RED)**: Distinct tickers with >= 522 bars is under 90.0%. Trigger emergency restore via `POST /api/admin/restore-now` with Bearer `ADMIN_TOKEN`.
3. **`broker_feed` (RED)**: INDMoney token expired (HTTP 403) or zero live quotes received. Re-authenticate INDMoney credentials.
4. **`recent_errors` (RED)**: More than 5 ERROR/CRITICAL logs recorded in the last 15 minutes. Inspect logs in `/api/health/deep`.
5. **`alerts` (RED)**: NTFY token rejected (HTTP 401/403) or misconfigured. Check `NTFY_AUTH_TOKEN`.

---

## 5. Startup & Operational Sequence

1. **Cold Startup & Per-Symbol Restore**:
   - At startup, the server calculates history coverage across 778 distinct tickers in the universe.
   - Symbols with recent listing history (< 40 calendar days since first bar and >= 50 bars) qualify under `LISTING_TOO_NEW`.
   - If coverage < 90%, it restores candle history per symbol from Firestore **BEFORE** the scanner starts.
   - Paced Yahoo backfill only downloads symbols still missing after Firestore restore.
   - Dispatches one priority startup NTFY alert: `"[STARTUP] version <hash> online | Firebase connected | history X% | token OK"`.
2. **08:30 IST Pre-Market Restore Check**: Re-evaluates coverage and pulls any newly synced bars from Firestore.
3. **08:45 IST Preflight Check**: Evaluates 10 preflight checkpoints and dispatches status NTFY.
4. **09:15 – 09:30 IST**: Market opens. INDmoney feeds live ticks into `CandleStore`.
5. **09:30 IST**: First 15m candle closes. Live scans begin with 60-second hold buffer and single daily latch per symbol.
6. **15:45 IST**: Post-market sync flushes all newly formed bars to `env/live/candles` in Firestore.

---

## 6. Rollback Instructions

If unexpected errors occur on Render after deployment:
1. Identify the previous stable release commit or branch: `release/v2.7.2` (commit `820afdc`).
2. In Render Dashboard:
   - Go to **Settings** -> **Build & Deploy** -> Set Branch to `release/v2.7.2`.
   - Click **Manual Deploy** -> **Clear build cache & deploy**.
3. Verify `/api/health/deep` returns HTTP 200 with code version `820afdc`.

---

## 7. What is NOT a Problem (Expected Behaviors)

- **`"market_open": false` outside 09:15 - 15:30 IST**: Normal off-hours behavior; health returns `HEALTHY_OFF_HOURS` (HTTP 200).
- **`SKIPPED_MARKET_CLOSED` on Weekends/Holidays**: All market bulletins are marked skipped with reason `Market closed (Weekend)` or `Market closed (NSE Holiday: ...)`.
- **`yfinance Legacy Scan Calls > 0`**: The legacy Yahoo path is intentionally ON (`ENABLE_LEGACY_YAHOO_SCAN_PATH=true`) as a safety valve. It has a separate 3,000 call/day ceiling and does not deplete the strict budget.
- **Render Ephemeral Disk Warning**: The warning disappears once `FIREBASE_CREDENTIALS_JSON` is active and connected.
- **Render Free 512 MB Memory**: App runs efficiently at ~122.7 MB peak RSS through streaming SQLite inserts and lazy loading.
