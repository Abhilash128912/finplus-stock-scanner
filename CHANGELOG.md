# 📑 APPLICATION CHANGELOG: FINPLUS+ STOCK & OPTIONS SCANNER (APP-02)

All notable changes to **FinPlus+ Stock & Options Ignition Scanner (APP-02)** are documented in this file.
Format adheres to [Keep a Changelog](https://keepachangelog.com/en/1.0.0/), adhering to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [2.5.2] - 2026-11-13

### Fixed
- **P0 - Close Failure Rollback (`LivePaperTracker`)**:
  - Implemented transactional rollback if database insertion fails (e.g., disk full, database locked). Restores position to `active_positions` as `OPEN`, logs `ERROR` with `trade_id`, and retries close on the subsequent poll cycle.
  - Suppressed all "PAPER TRADE CLOSED" logs and alerts until `rowcount == 1`.
  - Logged duplicate inserts (`rowcount == 0`) at `DEBUG` with zero alerts.
- **P0 - Schedule Ledger Unique Constraints & IN_FLIGHT Auto-Recovery (`ScheduleLedger`)**:
  - Added strict `UNIQUE (date_str, slot_id)` constraint with `INSERT OR IGNORE`.
  - On startup and initialization, automatically resets any stuck `IN_FLIGHT` rows older than 2 minutes back to `PENDING` (maintaining the maximum 3 attempts rule).
  - Enforced that milestones marked `COMPLETED` can never be re-claimed or re-sent.
- **P1 - NTFY Tiered Hard Ceiling Quota (`NTFYDispatcher`)**:
  - Established tiered daily caps reset strictly by IST calendar date:
    - Routine alerts stop at 220.
    - Trade open and close alerts are permitted up to 240.
    - Emergency stop-loss alerts exclusively use quota slots 241 to 248.
    - Hard ceiling at 248 completely blocks all outgoing pushes and logs a single `WARNING`.
- **P1 - Decoupled Commodity (MCX) Session Cutoffs (`LivePaperTracker`, `NTFYDispatcher`)**:
  - Excluded MCX positions (e.g. `GOLDPETAL`) from the 15:00 IST equity square-off.
  - Implemented configurable MCX cutoff (default 23:15 IST) via `MCX_CUTOFF_HOUR` and `MCX_CUTOFF_MINUTE`.
  - Separated Equity and MCX reviews in the 15:00 summary dispatch.
- **P0 - Price Sanity Guard & DATA_SUSPECT Circuit Breaker (`LivePaperTracker`)**:
  - Rejects any price tick or option premium moving $> 15\%$ (configurable via `PRICE_SANITY_MAX_PCT`) from the last accepted price in a single cycle or exceeding 3x entry cap. Logs `WARNING`, skips tick, and leaves P&L untouched.
  - Automatically flags symbols with 3 consecutive rejected ticks as `DATA_SUSPECT` and blocks fresh signals.
- **P1 - Editable Holiday and Weekend Calendar (`indian_market_calendar.py`, `config/nse_holidays.json`, `ScannerScheduler`)**:
  - Created editable `config/nse_holidays.json` containing full official exchange holidays and special trading sessions (e.g. Diwali Muhurat trading).
  - Skips scheduler bulletins, 08:45 AM IST token refresh, and signal dispatches on weekends and official holidays.
  - Logs startup status line: `"Market closed today: reason"`.
- **P1 - Deep Health Diagnostics Off-Hours & Per-Subsystem Telemetry (`/api/health/deep`, `APIServer`)**:
  - Returns HTTP 200 outside market hours, weekends, and holidays with `"market_open": false`.
  - During market hours, returns HTTP 503 only if the broker feed is stale, scheduler is stalled, or token is expired.
  - Evaluates cached token expiration without issuing broker HTTP requests per health ping.
  - Exposes discrete per-subsystem health states (`scheduler`, `broker`, `feed`, `ledger`, `notifications`).
- **P2 - Yahoo Finance Exponential Backoff & CandleStore Fallback (`MarketDataFeed`, `ScannerScheduler`)**:
  - On failed or rate-limited Yahoo candle chunks, retries up to 2 times with exponential backoff (2s, 6s).
  - Seamlessly falls back to local `CandleStore` bars for affected tickers, tagging fallback tickers in scan telemetry.
  - Dispatches an emergency NTFY alert (priority, max once per hour) if $> 10\%$ of universe tickers fall back to `CandleStore`.

---

## [2.5.1] - 2026-11-13

### Fixed
- **P0 - Thread-Safe Atomic Paper Position Closure & Single-Insert Guard (`LivePaperTracker`)**:
  - Removed positions from in-memory `active_positions` under a threading lock *before* initiating database operations, eliminating race conditions during fast square-off sweeps.
  - Switched database insertion to `INSERT OR IGNORE INTO closed_paper_trades` with `cursor.rowcount == 1` validation, preventing duplicate trade entries and whole-loop aborts.
  - Wrapped each trade evaluation in `update_and_trail_positions()` in its own isolated `try/except` block to ensure an individual trade failure never stops the tracker loop.
  - Only emit `"PAPER TRADE CLOSED"` logs and dispatch NTFY close alerts if the trade row was actually inserted (`rowcount == 1`).
  - Switched 15:00 summary P&L and total realized P&L calculations to query directly from the SQLite database (`closed_paper_trades`) instead of in-memory or logged closes, fixing the exact Rs 193.05 duplicate-close overstatement.
- **P0 - Unified Token Persistence, Pre-Flight Cooldown & Single-Flight Async Lock (`INDMoneyAuthClient`, `INDMoneyLiveFeed`)**:
  - Established a single canonical token store at `config/indmoney_token.json` holding `token`, `expiry`, and `last_request_time`, synchronized across all scripts and processes.
  - Enforced client-side cooldown verification *before* every broker request; during active cooldown, cached quotes or CandleStore data are served with zero external broker calls.
  - Replaced multi-threaded 403 storm with single-flight refresh lock: the first 403 triggers TOTP refresh while all other callers wait and reuse the new credentials, retrying failed chunks once.
  - Replaced blocking `time.sleep` with `asyncio.sleep` in async refresh paths to protect the event loop.
  - Converted `INDMoneyLiveFeed` into a thread-safe singleton, eliminating dual instantiation conflicts between `ScannerScheduler` and `LivePaperTracker`.
- **P0 - Proactive Token Warmup & Feed Staleness Circuit Breaker (`ScannerScheduler`)**:
  - Added proactive 08:45 AM IST token renewal on trading weekdays to guarantee valid session tokens before the 09:15 AM market open.
  - Implemented 60-second staleness watchdog during market hours: if token refresh fails or quotes are older than 60 seconds, output is tagged `STALE_FEED`, new directional signals are blocked (`signal_direction = 0`, `regime_confluence = "VETOED (STALE_FEED)"`), and broker polling halts until cooldown expires.
- **P1 - Persistent Daily NTFY Quota & Priority Alert Bypass (`NTFYDispatcher`, `ScheduleLedger`)**:
  - Added persistent SQLite table `ntfy_daily_quota` in `data/schedule_ledger.db` tracking messages sent per date, guaranteeing message counter survival across application restarts and redeploys.
  - Enabled priority bypass for critical money-protective alerts (Stop-Loss, Trade Close, Trade Open), ensuring risk alerts are never blocked even when the 220 daily message cap is reached.
  - Exposed live `ntfy_quota` metrics (`used`, `remaining`, `cap`) in `/api/health/deep` for monitoring.
- **P2 - Safe ASCII Headers & Secrets Sanitization (`NTFYDispatcher`, `CHANGELOG.md`, `.gitignore`)**:
  - Implemented `safe_headers()` utility that scrubs non-ASCII characters from HTTP headers (forcing clean ASCII and moving emojis strictly to UTF-8 message bodies), preventing `UnicodeEncodeError: 'latin-1' codec can't encode character` crashes.
  - Sanitized hardcoded NTFY tokens and token prefixes across documentation and codebases, defaulting to the `NTFY_AUTH_TOKEN` environment variable.
  - Added `config/indmoney_token.json` and `*token*.json` to `.gitignore`.

---

## [2.5.0] - 2026-11-13

### Added
- **State-Driven Persistent Schedule Ledger (`ScheduleLedger`)**: Built persistent SQLite-backed state machine (`data/schedule_ledger.db`) for all scheduled daily market bulletins (`09:00_AGENDA`, `09:30_PULSE_FIRST`, 30m Pulses, `15:00_SUMMARY`). Replaced rigid legacy 5-minute time windows (`0 <= mm < 5`) with state tracking (`PENDING`, `IN_FLIGHT`, `COMPLETED`, `FAILED`, `EXPIRED_STALE`), guaranteeing execution even through server restarts, latency, or deployment reboots.
- **Restored Full 750-Stock Quality Cash Universe (858 Total Instruments)**: Upgraded `BroadMarketMaster` with an automated liquidity cascade that supplements liquid Mid/Smallcap equities up to exactly 750 verified cash stocks (Min Price ₹50, Min Turnover ₹15 Cr). Regenerated and bundled `config/cash_stocks_master.json`. Total tracked universe now stands at **858 instruments** (750 Cash Equities + 108 F&O Options).
- **Permanent Self-Healing Headless TOTP Broker Authentication**: Upgraded `finplus_core/auth/indmoney_client.py` with RFC 6238 TOTP auto-regeneration. On any HTTP 401/403 or token expiration, the client automatically generates a fresh 24-hour token and persists it without manual intervention. Added active 60-second cooldown sleep handling to protect against broker rate limit lockouts (`{"message":"Please wait a minute before requesting another token."}`).
- **Zero-Delay Local SQLite CandleStore & Interactive Chart Engine**: Created `CandleStore` (`data/candles.db`) that captures live INDmoney broker ticks in real-time and synthesizes them into 15m and 5m OHLCV bars. Added `/api/candles/{symbol}` and an interactive TradingView Lightweight Charts canvas at `/chart/{symbol}`, eliminating reliance on delayed public widgets.
- **Deep Health Diagnostic Endpoint (`/api/health/deep`) for UptimeRobot**: Added diagnostic endpoint probing internal scheduler loops, universe size, broker token validity, and milestone ledger states. Returns HTTP 200 when fully healthy and HTTP 503 if any core subsystem stalls, providing actionable UptimeRobot alerting.
- **Automated Institutional Pre-Flight Verification Suite**: Created `test_institutional_preflight.py` verifying 6 key acceptance criteria (broker TOTP handshake, 750 cash universe, batch quote speed < 1s, candle synthesis, ledger catch-up, and decoupled feed integration).

### Fixed
- **Non-Blocking Asynchronous Dispatcher Threading**: Wrapped synchronous `requests` calls in `_clock_dispatcher_loop` with `asyncio.to_thread`, preventing network I/O from blocking the Uvicorn/FastAPI main event loop.
- **Stale Milestone Retirement & Retry Throttling**: Added milestone retry capping (`attempts < 3`) and automatic `EXPIRED_STALE` transitions for past-session milestones (> 35 mins late), preventing infinite retries and alert flooding on server restart.

---

## [2.4.0] - 2026-11-12

### Added
- **Multi-Threaded Chunked 15m Candle Prefetching**: Replaced single-threaded 1-by-1 Yahoo Finance requests for 527 tickers with chunked multi-threaded downloads (35 tickers/chunk). Reduces full scan cycle from 7+ minutes down to ~25 seconds and completely eliminates Yahoo Finance HTTP 429 `YFRateLimitError` lockouts.
- **Full INDmoney Broker Live Quote Feed (2,782 Scrips)**: Integrated official INDstocks broker live quote API (`https://api.indstocks.com/market/quotes/full`) with automatic dynamic mapping of 2,782 NSE EQ symbols from `equity_instruments.csv`. Provides 0-delay live broker LTP, Day High/Low, and Volume during scan cycles and paper trade execution.
- **Stage-2 High-Momentum Cash Breakout Engine (ZEEL Fix)**: Relaxed the blunt 1.2% base restriction for `INTRADAY_CASH` runners exhibiting explosive volume (`RVOL >= 2.0x` or `+2.5%` move with `RVOL >= 1.4x`). Classifies rapid momentum movers as `STAGE2_MOMENTUM_RUNNER` with active institutional triggers rather than falsely disqualifying them with `AWAITING_BASE_FORMATION` (confluence score 0).
- **Isolated Outlier Momentum Exception in Regime Interceptor**: Allowed high-RVOL cash equities (`RVOL >= 2.0x` or `confluence_score >= 80`) to trigger `APPROVED_ISOLATED_MOMENTUM` even when macro index breadth is in mild chop or minor downtrend (CTI down to -15), preventing red index days from suppressing isolated runner breakouts like ZEEL (+6%).

### Fixed
- **Alphabetical Fallback Slicing in BroadMarketMaster**: Removed early loop break (`len(items) >= top_n`) on `ind_nifty500list.csv`, preventing alphabetical truncation if the online fallback is invoked.
- **Cash Standby Reason Logic in ShortlistEngine**: Fixed bug where `cash_standby_reason` always reported "Market Choppy" from the watch list even when an active cash setup (e.g. ZEEL) was approved and recommended.

---

## [2.3.0] - 2026-11-12

### Added
- **Midday Theta Freeze Gate (11:30 AM – 01:00 PM IST)**: Implemented empirical timing filter in `shortlist_engine.py`, `live_paper_tracker.py`, and `ntfy_dispatcher.py`. Analyzed past trade journals and NSE market microstructure proving 11:30–13:00 is a high-decay dead-zone (0% win-rate). Fresh option buying and cash entries are strictly paused during this window, automatically resuming for the 13:00+ European open session.
- **Two-Way Intraday Cash Trading (MIS Short Selling + Long Breakouts)**: Expanded cash equity engine to recommend active `SELL / SHORT INTRADAY (MIS)` on quality, liquid non-F&O midcaps/smallcaps breaking down below support floors on high volume with tight SL (base roof) and 1:2.2 downside targets. Fully decoupled from F&O stock options (which exclusively trade CE/PE).
- **Dedicated NTFY Token Authentication**: Integrated official Bearer token via `NTFY_AUTH_TOKEN` environment variable across all dispatches in `ntfy_dispatcher.py` and `live_paper_tracker.py`, eliminating anonymous IP rate limiting.
- **Unified 220-Message Daily Safety Cap**: Raised message allowance to 220 messages/day (safely within the 250 free daily limit), preventing HTTP 429 quota lockouts.
- **Local Dev Push Suppression**: Suppressed local duplicate NTFY pushes so only 24/7 Render Cloud dispatches to user devices, protecting quota during development.

### Fixed
- **Unthrottled Direct NTFY Calls in Paper Tracker**: Fixed bug where `live_paper_tracker.py` made raw `requests.post()` calls on trade open/close that bypassed `NTFYDispatcher` rate limiting, which caused the 09:48 AM 429 lockout.
- **MIDCAPNIFTY Option Price Modeling & Phantom ₹3L Profit**: Corrected underlying index mapping from `^NSEI` (25,340) to `NIFTY_MID_SELECT.NS` (~14,250), added hard option surge ceiling (max 3.0x entry premium), and purged corrupt ledger entries.
- **Better Stack 405 Method Not Allowed**: Added native `HEAD` HTTP method handling across all API server routes in `finplus_index_breadth` and `finplus_stock_scanner` to satisfy free-tier monitoring pings.

---

## [2.2.0] - 2026-11-11

### Added
- **Institutional Micro-Base VCP Pre-Breakout Engine**: Eradicated blunt percentage filters (e.g. barring stocks > 1.5% or > 2.5%). Added micro-base compression detection (3–6 bar consolidation, base width $\le 1.2\%$) catching pre-ignition coiling 0.1%–0.7% below resistance before the move starts.
- **Stage-2 Momentum Continuation (NETWEB Scenario)**: Support for runners already up +3% to +8% that form tight consolidation launchpads above VWAP for secondary +2% to +3% legs.
- **4-Horizon Quantitative Scoring Engine**: Independent 0–100 scorecards for Intraday, Swing, Long-term Wealth, and Penny/4H segments.
- **Windows File Lock Fault Tolerance**: Built `SafeRotatingFileHandler` in `time_and_logger.py` to gracefully handle Windows `PermissionError` [WinError 32] during rapid multithreaded log rotation.

### Fixed
- **BSE Sensex Spot Resolution**: Resolved stale 80,757 display by purging hardcoded fallbacks and verifying real-time BSE feed synchronization (82,353+).

---

## [2.1.0] - 2026-11-08

### Added
- **30-Minute Institutional Best-in-Class Shortlist Card:** Dedicated UI card on `http://127.0.0.1:8001` highlighting the #1 Best Index Option (BSE Sensex prioritized / Nifty), #1 Best Stock Option (Lot <= 500 + Reliance), and #1 Intraday Cash Stock.
- **Granular 0–100 Point System:** Every candidate evaluated receives an explicit score with a transparent breakdown:
  - Range Breakout: 35 pts
  - PDH / PDL Confluence: 30 pts
  - VWAP Alignment: 20 pts
  - Relative Volume ($RVOL \ge 1.25\times$): 15 pts
- **Automated Trend Continuation Tracking:** If the previous 30-minute recommendation is still holding above/below VWAP and maintaining structure, NTFY alerts automatically flag `🔄 TREND CONTINUATION` and advise trailing stops rather than re-buying or overtrading.
- **Strict 30-Minute NTFY Throttling:** Consolidates all signals into one periodic alert every 30 minutes (09:30, 10:00, 10:30... IST) to completely prevent alert spam and overtrading. If no candidate reaches 50+ score, dispatches `🛑 HOLD CASH`.

---

## [2.0.0] - 2026-11-08

### Added
- **Fail-Closed Veto Gate:** Integrated App #1 Air Traffic Control Interceptor (`regime_interceptor.py`) with strict 2.0-second timeout. If App #1 is down or slow, the alert is tagged `VETOED_GATE_TIMEOUT`, never `CONFIRMED`.
- **Dynamic NSE F&O Master Ingestion:** Created `fno_contract_master.py` to ingest official `https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv` at startup, resolving official NSE symbols (`INFY`, `TCS`, `MARUTI`, `RELIANCE`) and SEBI-revised lot sizes.
- **Closed-Candle Evaluation:** Sliced `df.iloc[:-1]` in `range_detector_engine.py` to evaluate strictly on completed 15m bars, eliminating phantom signals that appear and disappear mid-candle.
- **Unified CTI Confluence Rule:** Confluence verified directly against App #1's Composite Trend Index ($\ge +30$ for Calls, $\le -30$ for Puts).
- **Dual-Timezone Compliance:** Logs and internal records strictly in ISO-8601 UTC ('Z'); exchange market hours in IST (`Asia/Kolkata`).
- **Web Cockpit & Desktop Floater:** Fast modern dashboard on `http://127.0.0.1:8001` and floating HUD on `/static/floater.html`.
- **Automated Test Suite:** 5 unit and integration tests passing in `tests/test_stock_scanner.py`.

