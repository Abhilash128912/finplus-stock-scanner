"""
scripts/measure_memory.py
Measures peak RSS memory for:
1. Cold restore from Firestore into a fresh SQLite store
2. One full universe scan cycle
"""

import os
import sys
import time
from pathlib import Path

# Add project root to sys.path
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.utils.memory_tracker import get_process_rss_mb
from src.storage.candle_store import CandleStore
from src.storage.firestore_backend import FirestoreBackend
from src.scheduler.scanner_scheduler import ScannerScheduler

def run_benchmark():
    print("================================================================================")
    print("FINPLUS+ PROCESS MEMORY (RSS) BENCHMARK")
    print("================================================================================")
    
    rss_start = get_process_rss_mb()
    print(f"[BASELINE] Process Start RSS: {rss_start:.1f} MB")
    
    # 1. Benchmark Cold Restore Simulation
    print("\n--- 1. Cold Restore Benchmark ---")
    data_tmp = _ROOT / "data" / "benchmark_temp"
    data_tmp.mkdir(parents=True, exist_ok=True)
    temp_cs = CandleStore(db_path=str(data_tmp / "candles_bench.db"))
    
    fb = FirestoreBackend()
    rss_pre_restore = get_process_rss_mb()
    
    if fb.is_connected:
        print("Connected to Firestore. Executing real restore benchmark...")
        t0 = time.time()
        res = fb.restore_all_from_firestore(temp_cs)
        t_dur = time.time() - t0
        rss_post_restore = get_process_rss_mb()
        print(f"Restore Completed: {res.get('candles_restored', 0)} bars across {res.get('symbols_restored', 0)} symbols in {t_dur:.2f}s")
        print(f"[COLD RESTORE] Pre-Restore RSS: {rss_pre_restore:.1f} MB | Post-Restore RSS: {rss_post_restore:.1f} MB | Delta: +{rss_post_restore - rss_pre_restore:.1f} MB")
    else:
        print("Firestore running in local-only / test mode. Simulating 500,000 bar matrix restore...")
        t0 = time.time()
        # Insert 500,000 bars in batches to test peak memory during massive sqlite insertions
        records = []
        for i in range(1000):
            records.append({
                "time": 1700000000 + i * 900,
                "open": 100.0,
                "high": 105.0,
                "low": 95.0,
                "close": 102.0,
                "volume": 1000
            })
        import pandas as pd
        df_sample = pd.DataFrame(records)
        df_sample["Open"] = df_sample["open"]
        df_sample["High"] = df_sample["high"]
        df_sample["Low"] = df_sample["low"]
        df_sample["Close"] = df_sample["close"]
        df_sample["Volume"] = df_sample["volume"]
        df_sample["Datetime"] = pd.to_datetime(df_sample["time"], unit="s")
        with temp_cs._get_connection() as conn:
            for s_idx in range(500):
                sym = f"SYM_{s_idx}"
                rows = [
                    (sym, "15m", int(r["time"]), "2026-11-01T00:00:00Z", "2026-11-01 05:30:00", float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]), int(r["volume"]))
                    for r in records
                ]
                conn.executemany("""
                    INSERT OR REPLACE INTO candles
                    (symbol, timeframe, epoch, timestamp_utc, timestamp_ist, open, high, low, close, volume)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, rows)
            conn.commit()
        t_dur = time.time() - t0
        rss_post_restore = get_process_rss_mb()
        print(f"Simulation Completed: 500,000 bars across 500 symbols in {t_dur:.2f}s")
        print(f"[COLD RESTORE SIMULATION] Pre-Restore RSS: {rss_pre_restore:.1f} MB | Post-Restore RSS: {rss_post_restore:.1f} MB | Delta: +{rss_post_restore - rss_pre_restore:.1f} MB")

    # Clean up temp db
    try:
        import shutil
        shutil.rmtree(data_tmp, ignore_errors=True)
    except Exception:
        pass

    # 2. Benchmark One Full Universe Scan Cycle (Mocked 97 Instruments for Meaningful Off-Hours Telemetry)
    print("\n--- 2. Full Universe Scan Cycle Benchmark (97 Mocked Instruments) ---")
    rss_pre_scan = get_process_rss_mb()
    scheduler = ScannerScheduler()
    
    # Generate 97 mock instruments with 550 bars of synthetic data
    mock_universe_97 = []
    sample_records = []
    for b in range(550):
        sample_records.append({
            "time": 1700000000 + b * 900,
            "open": 100.0 + (b % 10) * 0.5,
            "high": 105.0 + (b % 10) * 0.5,
            "low": 95.0 + (b % 10) * 0.5,
            "close": 102.0 + (b % 10) * 0.5,
            "volume": 1000 + b * 10
        })
    import pandas as pd
    df_sample = pd.DataFrame(sample_records)
    df_sample["Open"] = df_sample["open"]
    df_sample["High"] = df_sample["high"]
    df_sample["Low"] = df_sample["low"]
    df_sample["Close"] = df_sample["close"]
    df_sample["Volume"] = df_sample["volume"]
    df_sample["Datetime"] = pd.to_datetime(df_sample["time"], unit="s")
    df_sample.attrs["data_source"] = "MOCK_BENCHMARK"

    from unittest.mock import patch, MagicMock

    for i in range(97):
        sym = f"FO_SYM_{i:02d}"
        mock_universe_97.append({
            "symbol": sym,
            "yf_ticker": f"{sym}.NS",
            "category": "STOCK" if i > 0 else "INDEX",
            "lot_size": 250
        })

    # Mock market open so scanning executes fully even on weekends/off-hours
    with patch("src.scheduler.scanner_scheduler.is_equity_market_open_ist", return_value=(True, "Market Open (Benchmark)")):
        scheduler.feed.get_all_universe_items = MagicMock(return_value=mock_universe_97)
        scheduler.feed.fetch_15m_candles = MagicMock(return_value=df_sample)
        scheduler.feed.prefetch_batch_15m_candles = MagicMock()
        if scheduler.indmoney_feed:
            scheduler.indmoney_feed.fetch_batch_quotes = MagicMock(return_value={
                u["symbol"]: {"ltp": 102.5, "day_high": 105.0, "day_low": 95.0} for u in mock_universe_97
            })
        
        t0 = time.time()
        scan_res = scheduler.scan_universe_now()
        t_scan_dur = time.time() - t0
        rss_post_scan = get_process_rss_mb()

    print(f"Universe Scan Completed in {t_scan_dur:.2f}s (Scanned: {len(scan_res)} items)")
    print(f"[SCAN CYCLE (97 INSTRUMENTS)] Pre-Scan RSS: {rss_pre_scan:.1f} MB | Post-Scan RSS: {rss_post_scan:.1f} MB | Delta: +{rss_post_scan - rss_pre_scan:.1f} MB")
    
    # 3. Overall Peak
    rss_final = get_process_rss_mb()
    print("\n================================================================================")
    print(f"SUMMARY TELEMETRY:")
    print(f"- Process Peak RSS Observed: {rss_final:.1f} MB")
    print(f"- Render Free Tier Headroom (Limit: 512.0 MB): {512.0 - rss_final:.1f} MB safety buffer remaining")
    print("================================================================================")

if __name__ == "__main__":
    run_benchmark()
