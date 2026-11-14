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
        for s_idx in range(500):
            temp_cs.save_dataframe_candles(f"SYM_{s_idx}", "15m", df_sample)
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

    # 2. Benchmark One Full Universe Scan Cycle
    print("\n--- 2. Full Universe Scan Cycle Benchmark ---")
    rss_pre_scan = get_process_rss_mb()
    scheduler = ScannerScheduler()
    t0 = time.time()
    scan_res = scheduler.scan_universe_now()
    t_scan_dur = time.time() - t0
    rss_post_scan = get_process_rss_mb()
    
    print(f"Universe Scan Completed in {t_scan_dur:.2f}s (Scanned: {len(scan_res)} items)")
    print(f"[SCAN CYCLE] Pre-Scan RSS: {rss_pre_scan:.1f} MB | Post-Scan RSS: {rss_post_scan:.1f} MB | Delta: +{rss_post_scan - rss_pre_scan:.1f} MB")
    
    # 3. Overall Peak
    rss_final = get_process_rss_mb()
    print("\n================================================================================")
    print(f"SUMMARY TELEMETRY:")
    print(f"- Process Peak RSS Observed: {rss_final:.1f} MB")
    print(f"- Render Free Tier Headroom (Limit: 512.0 MB): {512.0 - rss_final:.1f} MB safety buffer remaining")
    print("================================================================================")

if __name__ == "__main__":
    run_benchmark()
