# INDstocks (INDmoney) Real Option Quote Integration

This module replaces synthetic option pricing with **real-time live market quotes** from the official INDstocks (INDmoney) API (`api.indstocks.com`) for all paper options trading in `finplus_stock_scanner`.

## Key Specifications & Architecture

### 1. Authentication
* **Token Source:** `INDSTOCKS_TOKEN` or `INDMONEY_ACCESS_TOKEN` in `.env` (or autonomous 24h TOTP generation via `finplus_core.auth.INDMoneyAuthClient`).
* **Header Format:** `Authorization: <token>` (no `Bearer` prefix).
* **Security:** Zero credential or token logging.

### 2. Daily Instrument Resolution
* **Endpoint:** `GET https://api.indstocks.com/market/instruments?source=fno`
* **Local Cache:** Cached daily in `data/cache/instruments_fno_YYYYMMDD.csv`.
* **Resolution Mapping:** Maps `(underlying, expiry, strike, CE/PE)` to the exact `SECURITY_ID` and scrip code (`NFO_<security_id>` for NSE, `BFO_<security_id>` for BSE Sensex).
* **Verified CSV Columns:**
  `['EXCH', 'SEGMENT', 'SECURITY_ID', 'INSTRUMENT_NAME', 'EXPIRY_CODE', 'TRADING_SYMBOL', 'LOT_UNITS', 'CUSTOM_SYMBOL', 'EXPIRY_DATE', 'STRIKE_PRICE', 'OPTION_TYPE', 'TICK_SIZE', 'EXPIRY_FLAG', 'SEM_EXCH_INSTRUMENT_TYPE', 'SERIES', 'SYMBOL_NAME', 'UNDERLYING_SCRIP_NAME', 'FREEZE_QTY', 'UPPER_LIMIT', 'LOWER_LIMIT', 'ISIN', 'GENERAL_FACTOR', 'DELIVERY_UNIT']`

### 3. Real-Time Quotes & Top-of-Book Depth
* **Endpoint:** `GET https://api.indstocks.com/market/quotes/full?scrip-codes=NFO_<token>`
* Extracts top-of-book depth level 1:
  * **Ask Price:** `market_depth[scrip_code]["depth"][0]["sell"]["price"]`
  * **Bid Price:** `market_depth[scrip_code]["depth"][0]["buy"]["price"]`
  * **LTP & OI:** `live_price` and `open_interest`
* **Paper Execution:** Fills entry strictly at `ask` and exits strictly at `bid`.

### 4. Illiquidity Filter (`SKIPPED_ILLIQUID`)
* Any option signal where `(ask - bid) / ask > 5.0%` or depth level 1 is empty is immediately skipped and logged as `SKIPPED_ILLIQUID`.

### 5. WebSocket Live Price Streaming
* **WebSocket Endpoint:** `wss://ws-prices.indstocks.com/api/v1/ws/prices`
* **Subscription:** While a trade is open, subscribes to only that active contract:
  ```json
  {"action": "subscribe", "mode": "quote", "instruments": ["NFO:<token>"]}
  ```
* **Unsubscription:** Unsubscribes immediately upon position exit.
* **Fallback:** If WebSocket drops, reconnects with exponential backoff while polling REST quotes every 5 seconds.

### 6. Greeks & IV Utility
* If quote lacks IV, calls `GET https://api.indstocks.com/market/option-chain?exchange=...&segment=INDEX&underlying-scrip=...&expiry=...&strike_count=5` throttled to at most once per 60 seconds per contract.

### 7. Periodic Telemetry Logging
* Every 60 seconds while an option trade is open, appends tick telemetry to SQLite table:
  ```sql
  option_ticks (trade_id, ts, bid, ask, ltp, iv, oi)
  ```

### 8. Source Attribution & P&L Reporting
* All positions and closed trades track `price_source = REAL_QUOTE | SYNTHETIC`.
* If quote fetch fails or broker is unavailable, falls back safely to the synthetic delta/theta model tagged `SYNTHETIC`.
* `LivePaperTracker.get_pnl_by_price_source()` breaks down trade counts, gross P&L, charges, net P&L, and win rate separately by price source.
