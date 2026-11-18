"""
FinPlus+ Backtest Only - History Backfill Runner
================================================
Standalone off-hours / backtesting utility to populate CandleStore.
NEVER imported or invoked by src/ runtime.
"""

import sys
import os
import time
from pathlib import Path

# Add project root and tools directory
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from tools.backtest_only.yfinance_gateway import YFinanceGateway
from src.storage.candle_store import CandleStore


def run_backtest_backfill(symbols: list, period: str = "60d", chunk_size: int = 20):
    """Backfills 15m historical candles for backtesting purposes only."""
    gateway = YFinanceGateway()
    candle_store = CandleStore()
    
    tickers = [f"{s}.NS" if not s.endswith(".NS") and not s.startswith("^") else s for s in symbols]
    print(f"Starting backtest backfill for {len(tickers)} symbols...")
    
    for i in range(0, len(tickers), chunk_size):
        chunk = tickers[i:i + chunk_size]
        print(f"Downloading chunk {i // chunk_size + 1}: {chunk}...")
        raw_df = gateway.download(chunk, reason="BACKTEST_BACKFILL", caller="history_backfill_runner", period=period, interval="15m")
        if raw_df is not None and not raw_df.empty:
            for t in chunk:
                clean = t.replace(".NS", "")
                if len(chunk) == 1:
                    candle_store.save_dataframe_candles(clean, "15m", raw_df)
                else:
                    try:
                        df = raw_df[t].dropna(how="all")
                        if not df.empty:
                            candle_store.save_dataframe_candles(clean, "15m", df)
                    except Exception as e:
                        print(f"Error parsing {clean}: {e}")
        time.sleep(2.0)
    print("Backtest backfill completed.")


if __name__ == "__main__":
    test_symbols = ["RELIANCE", "TCS", "INFY"]
    run_backtest_backfill(test_symbols)
