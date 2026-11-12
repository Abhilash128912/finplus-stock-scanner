# 📑 APPLICATION CHANGELOG: FINPLUS+ STOCK & OPTIONS SCANNER (APP-02)

All notable changes to **FinPlus+ Stock & Options Ignition Scanner (APP-02)** are documented in this file.
Format adheres to [Keep a Changelog](https://keepachangelog.com/en/1.0.0/), adhering to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
- **Dedicated NTFY Token Authentication (`tk_wu13o6...`)**: Integrated official Bearer token across all dispatches in `ntfy_dispatcher.py` and `live_paper_tracker.py`, eliminating anonymous IP rate limiting.
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

