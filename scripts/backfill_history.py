"""
FinPlus+ Institutional Historical Candle Backfill Script
======================================================
Backfills 30 trading sessions of 15m candles (~750 bars) across the 858-symbol universe.
- Resumable: Checks existing bar count in SQLite candles.db; skips symbols with >= 522 bars.
- Paced: Downloads in chunks of 25-30 tickers with a 15-second pause between chunks to respect Yahoo rate limits.
- Gateway Routed: Strictly uses YFinanceGateway with reason="HISTORY_BACKFILL".
- Progress Monitored: Outputs live progress, chunk timing, and final coverage matrix.
"""

import sys
import os
import time
from pathlib import Path
from typing import List, Dict, Any
import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.gateway.yfinance_gateway import YFinanceGateway
from src.storage.candle_store import CandleStore
from src.feeds.market_data_feed import MarketDataFeed
from src.utils.time_and_logger import setup_logger

logger = setup_logger("BackfillHistoryScript")

CHUNK_SIZE = 25
PAUSE_SECONDS = 15.0
TARGET_BARS = 522 # ATR 500 (500 + 20 + 2 = 522 bars)

def get_coverage_stats(candle_store: CandleStore, all_symbols: List[str]) -> Dict[str, Any]:
    with candle_store._get_connection() as conn:
        rows = conn.execute("""
            SELECT symbol, count(*) as cnt 
            FROM candles 
            WHERE timeframe = '15m' 
            GROUP BY symbol
        """).fetchall()
    
    counts = {r["symbol"].upper(): r["cnt"] for r in rows}
    
    total = len(all_symbols)
    atr_14 = sum(1 for s in all_symbols if counts.get(s, 0) >= 36)
    atr_50 = sum(1 for s in all_symbols if counts.get(s, 0) >= 72)
    atr_100 = sum(1 for s in all_symbols if counts.get(s, 0) >= 122)
    atr_500 = sum(1 for s in all_symbols if counts.get(s, 0) >= TARGET_BARS)
    
    return {
        "total": total,
        "atr_14": atr_14,
        "atr_50": atr_50,
        "atr_100": atr_100,
        "atr_500": atr_500,
        "counts": counts
    }

def run_backfill(chunk_size: int = CHUNK_SIZE, pause_secs: float = PAUSE_SECONDS):
    print("=" * 70)
    print("🚀 FINPLUS+ INSTITUTIONAL CANDLE BACKFILL RUNNER")
    print("=" * 70)

    gateway = YFinanceGateway()
    candle_store = CandleStore()
    feed = MarketDataFeed()

    universe = feed.get_all_universe_items()
    universe_tickers = {}
    for item in universe:
        sym = item.get("symbol", "").split(" ")[0].upper()
        ticker = item.get("yf_ticker", f"{sym}.NS")
        if sym and ticker:
            universe_tickers[sym] = ticker

    all_symbols = list(universe_tickers.keys())
    print(f"Loaded {len(all_symbols)} total universe symbols.")

    # Check existing coverage
    stats = get_coverage_stats(candle_store, all_symbols)
    print(f"Current ATR 500 Coverage (>=522 bars): {stats['atr_500']}/{len(all_symbols)} ({stats['atr_500']/len(all_symbols)*100:.1f}%)")

    # Filter out symbols that already have enough history
    missing_symbols = [s for s in all_symbols if stats["counts"].get(s, 0) < TARGET_BARS]
    print(f"Symbols requiring backfill: {len(missing_symbols)}")

    if not missing_symbols:
        print("✅ All symbols already have sufficient history! No backfill required.")
        return

    chunks = [missing_symbols[i:i + chunk_size] for i in range(0, len(missing_symbols), chunk_size)]
    print(f"Partitioned into {len(chunks)} chunks of up to {chunk_size} tickers (pacing: {pause_secs}s sleep).")
    print("-" * 70)

    start_time = time.time()
    for idx, chunk_syms in enumerate(chunks, 1):
        chunk_tickers = [universe_tickers[s] for s in chunk_syms]
        chunk_start = time.time()
        print(f"[{idx}/{len(chunks)}] Downloading {len(chunk_tickers)} tickers: {chunk_syms[:3]}...", end=" ", flush=True)

        try:
            raw_df = gateway.download(
                chunk_tickers,
                reason="HISTORY_BACKFILL",
                caller="BackfillHistoryScript",
                trigger_detail=f"Chunk {idx}/{len(chunks)}",
                period="30d",
                interval="15m",
                group_by="ticker",
                threads=True
            )
        except Exception as e:
            print(f"❌ Error: {e}")
            raw_df = None

        saved_count = 0
        if raw_df is not None and not raw_df.empty:
            if len(chunk_tickers) == 1:
                t = chunk_tickers[0]
                sym = chunk_syms[0]
                df = raw_df.copy()
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)
                candle_store.save_dataframe_candles(sym, "15m", df)
                saved_count = len(df)
            else:
                for sym, t in zip(chunk_syms, chunk_tickers):
                    try:
                        if hasattr(raw_df.columns, "levels") and t in raw_df.columns.levels[0]:
                            df = raw_df[t].dropna(how="all").copy()
                            if not df.empty:
                                candle_store.save_dataframe_candles(sym, "15m", df)
                                saved_count += len(df)
                    except Exception:
                        pass

        chunk_dur = time.time() - chunk_start
        print(f"Saved {saved_count} total bars ({chunk_dur:.1f}s).", flush=True)

        # Check interim coverage
        current_stats = get_coverage_stats(candle_store, all_symbols)
        cov_pct = current_stats["atr_500"] / len(all_symbols) * 100.0
        print(f"   -> Universe Coverage: {current_stats['atr_500']}/{len(all_symbols)} ({cov_pct:.1f}%)")

        if idx < len(chunks):
            time.sleep(pause_secs)

    total_time = time.time() - start_time
    print("=" * 70)
    print(f"🏁 BACKFILL COMPLETE in {total_time/60.0:.1f} minutes.")
    final_stats = get_coverage_stats(candle_store, all_symbols)
    print("FINAL COVERAGE REPORT:")
    print(f"  Total Universe: {len(all_symbols)} symbols")
    print(f"  ATR 14  (>= 36 bars):  {final_stats['atr_14']}/{len(all_symbols)} ({final_stats['atr_14']/len(all_symbols)*100:.1f}%)")
    print(f"  ATR 50  (>= 72 bars):  {final_stats['atr_50']}/{len(all_symbols)} ({final_stats['atr_50']/len(all_symbols)*100:.1f}%)")
    print(f"  ATR 100 (>= 122 bars): {final_stats['atr_100']}/{len(all_symbols)} ({final_stats['atr_100']/len(all_symbols)*100:.1f}%)")
    print(f"  ATR 500 (>= 522 bars): {final_stats['atr_500']}/{len(all_symbols)} ({final_stats['atr_500']/len(all_symbols)*100:.1f}%)")
    print("=" * 70)

if __name__ == "__main__":
    pause = float(os.getenv("BACKFILL_PAUSE_SECONDS", "15.0"))
    csize = int(os.getenv("BACKFILL_CHUNK_SIZE", "30"))
    run_backfill(chunk_size=csize, pause_secs=pause)

