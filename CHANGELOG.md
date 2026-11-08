# 📑 APPLICATION CHANGELOG: FINPLUS+ STOCK & OPTIONS SCANNER (APP-02)

All notable changes to **FinPlus+ Stock & Options Ignition Scanner (APP-02)** are documented in this file.
Format adheres to [Keep a Changelog](https://keepachangelog.com/en/1.0.0/), adhering to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
