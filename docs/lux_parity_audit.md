# LuxAlgo Range Detector Parity & Deviation Audit
**Document Version**: 2.5.4 / 2.6.0  
**Date**: 2026-11-13  
**Status**: ACTIVE REFERENCE SPECIFICATION

---

## 1. Mathematical Parity Audit Table (Items 1 - 14)

| # | Indicator / Function | LuxAlgo Pine Script Definition | Python Implementation (`range_detector_engine.py`) | Status | Notes / Deviations |
|---|---|---|---|---|---|
| 1 | **ATR True Range Calculation** | `math.max(high - low, math.abs(high - close[1]), math.abs(low - close[1]))` | `tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)` | **MATCH** | Exact parity across all 3 TR components. |
| 2 | **RMA Seeding & Recurrence** | `ta.rma(tr, length)`: SMA of first `length` true ranges, then `rma = (prev * (length - 1) + tr) / length` | `calc_rma()`: First value is SMA of first `length` items, then exact recurrence formula | **MATCH** | Replaced `ewm(adjust=False)` seeding with exact Pine SMA seeding. Maximum difference < 0.0001. |
| 3 | **SMA Centerline** | `ta.sma(close, length)` (default length: 20) | `closed_df['Close'].rolling(self.length).mean()` | **MATCH** | Rolling arithmetic mean over closed candle series. |
| 4 | **Filter Count Condition** | `count = math.sum(math.abs(close - sma) > atr, length)` | Iterative loop over `range(self.length)` checking `abs(closes[n - i] - curr_ma) > curr_atr` | **MATCH** | Exactly replicates filter check for coiling consolidation bars. |
| 5 | **Box Detection Trigger** | `count == 0 and prev_count != 0` | `if count == 0 and prev_count != 0:` | **MATCH** | Identifies new compression box formation. |
| 6 | **Box Overlap & Merge** | `if (n - length) <= bx_right and not na(bx_top): bx_top := math.max(new_top, bx_top); bx_bottom := math.min(new_bottom, bx_bottom)` | `bx_top = max(curr_ma + curr_atr, bx_top); bx_bottom = min(curr_ma - curr_atr, bx_bottom)` | **MATCH** | Overlapping compression boxes cleanly merge; `range_mid = (range_top + range_bot) / 2.0`. |
| 7 | **Break Window Evaluation** | LuxAlgo standard evaluates price relative to active box | `if not np.isnan(bx_top) and n <= bx_right + self.break_window_bars:` (configurable default 5) | **DEVIATES** | **Intentional Architectural Deviation**: Break window extended to 5 bars (with shadow variants for 1 and 2 bars) to prevent premature box invalidation during tight consolidations. |
| 8 | **Oscillator State (`os_state`)** | `if os == 0: if close > bx_top -> os := 1; if close < bx_bottom -> os := -1` | Replicated with state persistence over `break_window_bars` | **DEVIATES** | Tied to intentional 5-bar break window to allow continuation tracking before state reset. |
| 9 | **Candle-Close Evaluation** | Evaluates on completed bars | `closed_df = df.iloc[:-1].copy()` | **MATCH** | Mandatory rule: Forms and levels derived strictly from closed bars; forming candle never touches detector. |
| 10 | **Minimum History Rule** | Requires sufficient bars for RMA warmup | `min_history_bars = atr_len + range_len + 2` | **MATCH** | Explicit count and skip reason when insufficient history; never shortens ATR silently. |
| 11 | **Micro-Base Consolidation Overlay** | *Not present in basic Pine LuxAlgo script* | Last 3-6 bar tight consolidation (< 1.2% width) near HOD | **EXTENSION** | **Proprietary Institutional Extension**: Identifies pre-breakout coils and Stage-2 momentum continuation runners (e.g. NETWEB, ZEEL). |
| 12 | **50 EMA Confluence Gate** | *Not present in basic Pine LuxAlgo script* | `ema50 = closed_df['Close'].ewm(span=50).mean()`; proximity gate (2.0% Index, 3.5% Stock) | **EXTENSION** | Trend alignment filter to disqualify counter-trend or overextended breakouts. |
| 13 | **Intraday VWAP Confluence** | *Not present in basic Pine LuxAlgo script* | `calc_intraday_vwap(closed_df)` with daily session reset | **EXTENSION** | Multi-factor institutional score weighting. |
| 14 | **Daily Breakout Deduplication Latch** | *Not present in basic Pine LuxAlgo script* | Persistent SQLite table `daily_breakout_latch (date_str, symbol)` | **EXTENSION** | Prevents repeat alerts for the same symbol across multiple scan cycles on the same trading day. |

---

## 2. Summary of Deviations and Extensions

1. **Items 7 & 8 (`BREAK_WINDOW_BARS`)**:
   - LuxAlgo default drops the box immediately or tracks single-bar state transitions. APP-02 intentionally supports a configurable `break_window_bars` (default: 5) to allow high-probability momentum entries within 1 to 5 bars of the initial compression break.
2. **Item 11 (Micro-Base Overlay)**:
   - Proprietary enhancement that detects secondary consolidation pivots inside the 15m session, allowing earlier entries before wide ATR moves are exhausted.
3. **Items 12, 13 & 14 (Confluence Gates & SQLite Latch)**:
   - Institutional reliability layers: 50 EMA and VWAP prevent fakeouts, and the SQLite latch enforces strict single-alert idempotency per symbol per day.

