# Comprehensive Audit of `yfinance` Usage
**FinPlus+ Stock Scanner (APP-02)**  
**Audit Date**: 2026-11-13  
**Status**: ACTIVE AUDIT REFERENCE FOR v2.5.4 GATEWAY CONSOLIDATION

---

## 1. Summary of All Direct `yfinance` Imports in Production Code

Prior to v2.5.4 gateway migration, `yfinance` was imported directly in four distinct production modules:
1. `src/feeds/market_data_feed.py` (line 10)
2. `src/paper/live_paper_tracker.py` (line 21)
3. `src/api/server.py` (lines 309, 336)
4. `src/notifications/ntfy_dispatcher.py` (line 237)

---

## 2. Exhaustive Usage Matrix

| File & Line | Target Asset / Ticker | Method / Attribute Called | Frequency & Trigger | Issue / Migration Plan |
|---|---|---|---|---|
| `src/feeds/market_data_feed.py:87` | 858 Universe stocks (`chunks of 35`) | `yf.download(chunk, period="5d", interval="15m", group_by="ticker", progress=False, threads=True)` | **Every scan cycle (every 60s)** during market hours in `prefetch_batch_15m_candles()`. | **CRITICAL DEVIATION**: Makes ~25-30 HTTP batch calls every minute, risking IP ban (HTTP 429). **Fix**: Completely remove from market-hours scan path. Closed candles must come from `CandleStore` (INDmoney ticks) and SQLite cache. |
| `src/feeds/market_data_feed.py:155` | Individual symbol (`ticker`) | `yf.download(ticker, period=period, interval="15m", progress=False)` | On-demand when symbol not found in memory cache. | **DEVIATION**: Bypasses gateway and has no reason code. **Fix**: Route through `yfinance_gateway.py` with `FALLBACK_GUARD` or `NEW_SYMBOL` reason code. |
| `src/feeds/market_data_feed.py:64` | Arbitrary ticker | `yf.Ticker(ticker)` | Helper method `get_ticker_data()`. | **Fix**: Route through gateway; remove direct ticker handle creation. |
| `src/paper/live_paper_tracker.py:507-525` | Commodities & FX: `USDINR=X`, `CL=F`, `NG=F`, `GC=F` | `yf.Ticker(...).fast_info.last_price` | Every 5-second paper tracking loop during MCX hours. | **Risk**: Polls Yahoo every 5s for commodity spot conversion. **Fix**: Route through `yfinance_gateway.py` with caching and `FALLBACK_GUARD` reason code. INDmoney quotes used first whenever available. |
| `src/paper/live_paper_tracker.py:565` | Equity/Option underlying | `yf.Ticker(yf_ticker).fast_info.last_price` | In `_fetch_live_price()` when INDmoney batch quote fails. | **Fix**: Use INDmoney quotes first; only fall back to gateway if broker is stale > 3 minutes. |
| `src/api/server.py:309, 336` | `^BSESN` (Sensex Spot) | `yf.Ticker("^BSESN").fast_info.last_price` | On-demand on `/api/radar/sensex-nifty` calls when spot < 81500. | **Fix**: Check App #1 API and INDmoney quote first. If missing, route through gateway with `FALLBACK_GUARD`. |
| `src/notifications/ntfy_dispatcher.py:237` | `^BSESN` (Sensex Spot) | `yf.Ticker("^BSESN").fast_info` (`last_price`, `previous_close`) | Every 30 minutes in `send_30m_shortlist_pulse()` when Sensex spot < 81500. | **Fix**: INDmoney quotes used first. Route through gateway if fallback required. |

---

## 3. Strict Gateway Architectural Rules
1. **Single Importer**: `src/gateway/yfinance_gateway.py` is the **only** file permitted to import `yfinance`.
2. **Mandatory Reason Codes**:
   - `HISTORY_BACKFILL`: Scheduled once-per-day off-hours backfill.
   - `NEW_SYMBOL`: First-time onboarding of newly tracked instrument.
   - `FALLBACK_GUARD`: Broker stale > 3 minutes AND symbol has active open position or is in top-3 candidates.
3. **Daily Budget & Rate Ceiling**:
   - Max 250 requests per calendar day.
   - **429 Circuit Breaker**: If any Yahoo call returns HTTP 429, trip circuit breaker for **15 minutes**; block all Yahoo calls and fallback to cached data.
4. **Persistent Audit Ledger**:
   - Every single call is recorded in `yfinance_usage` SQLite table (`data/schedule_ledger.db`).
