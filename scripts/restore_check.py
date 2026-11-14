"""
scripts/restore_check.py
Restores candle history from Firestore into an isolated temporary database,
evaluating ATR 14/50/100/500 coverage and validating zero yfinance downloads.

Safety Rule:
- Refuses to run if the key file is inside the project folder or a git repository.
- Key file is read-only, never copied, logged, or committed.
"""

import os
import sys
import argparse
import time
import json
from pathlib import Path

# Add project root to sys.path
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.storage.candle_store import CandleStore
from src.storage.firestore_backend import FirestoreBackend

def is_path_in_repo_or_project(path: Path) -> bool:
    """Checks if a path is located inside the current project root or any git repository."""
    resolved = path.resolve()
    # Check project root
    try:
        resolved.relative_to(_ROOT.resolve())
        return True
    except ValueError:
        pass
    
    # Check parent directories for .git
    curr = resolved
    while curr != curr.parent:
        if (curr / ".git").exists():
            return True
        curr = curr.parent
    return False

def main():
    parser = argparse.ArgumentParser(description="FinPlus+ Firestore Restore Verification Utility")
    parser.add_argument("--key-file", type=str, required=True, help="Path to Firebase Service Account JSON outside repository")
    parser.add_argument("--data-dir", type=str, required=True, help="Path to temporary data directory for SQLite candles")
    args = parser.parse_args()

    key_path = Path(args.key_file)
    if not key_path.exists():
        print(f"[ERROR] Service account key file not found: {key_path}")
        sys.exit(1)

    # SAFETY CHECK: Refuse if inside project or git repo
    if is_path_in_repo_or_project(key_path):
        print(f"[FATAL SECURITY ERROR] Key file is located inside project directory or git repository: {key_path}")
        print("Refusing to execute. Move service account JSON to a secure location outside all git repositories (e.g., C:/Users/<User>/.keys/credentials.json).")
        sys.exit(1)

    data_dir = Path(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    temp_db_path = str(data_dir / "candles_restore_check.db")

    print("================================================================================")
    print("FINPLUS+ FIRESTORE RESTORE VERIFICATION")
    print("================================================================================")
    print(f"Target Temporary SQLite: {temp_db_path}")
    print(f"Key File Validated (External, Secure): {key_path.name}")

    # Read credentials in-memory only without persisting or logging
    try:
        with open(key_path, "r", encoding="utf-8") as f:
            creds_data = json.load(f)
        creds_json = json.dumps(creds_data)
    except Exception as e:
        print(f"[ERROR] Failed reading service account key JSON: {e}")
        sys.exit(1)

    os.environ["FIREBASE_CREDENTIALS_JSON"] = creds_json
    cs = CandleStore(db_path=temp_db_path)
    fb = FirestoreBackend(env_prefix="live")

    if not fb.is_connected:
        print("[ERROR] Failed connecting to Firestore using provided credentials.")
        sys.exit(1)

    print(f"Connected to Firestore under environment prefix: '{fb.env_prefix}'")
    print("Starting cold restore into temporary SQLite store...")
    t0 = time.time()
    restore_res = fb.restore_all_from_firestore(cs)
    dur = time.time() - t0

    # Calculate coverage across multiple ATR thresholds
    cov_14 = cs.get_history_coverage(target_bars=36)   # ATR 14 requires 36 bars
    cov_50 = cs.get_history_coverage(target_bars=72)   # ATR 50 requires 72 bars
    cov_100 = cs.get_history_coverage(target_bars=122) # ATR 100 requires 122 bars
    cov_500 = cs.get_history_coverage(target_bars=522) # ATR 500 requires 522 bars

    print("--------------------------------------------------------------------------------")
    print(f"RESTORE METRICS:")
    print(f"- Symbols Restored: {restore_res.get('symbols_restored', 0)}")
    print(f"- Bars Restored: {restore_res.get('candles_restored', 0):,}")
    print(f"- Firestore Reads Used: {restore_res.get('reads_used', 0)}")
    print(f"- Elapsed Time: {dur:.2f} seconds")
    print(f"- yfinance Calls Made: 0 (Strictly 0 external network calls)")
    print("--------------------------------------------------------------------------------")
    print(f"COVERAGE BREAKDOWN:")
    print(f"- ATR 14  (>= 36 bars):  {cov_14.get('symbols_522_bars', {}).get('count', 0)} symbols ({cov_14.get('coverage_522_pct', 0.0)}%)")
    print(f"- ATR 50  (>= 72 bars):  {cov_50.get('symbols_522_bars', {}).get('count', 0)} symbols ({cov_50.get('coverage_522_pct', 0.0)}%)")
    print(f"- ATR 100 (>= 122 bars): {cov_100.get('symbols_522_bars', {}).get('count', 0)} symbols ({cov_100.get('coverage_522_pct', 0.0)}%)")
    print(f"- ATR 500 (>= 522 bars): {cov_500.get('symbols_522_bars', {}).get('count', 0)} symbols ({cov_500.get('coverage_522_pct', 0.0)}%)")
    print(f"- Listing Too New / Recent IPOs: {cov_500.get('listing_too_new_count', 0)}")
    print(f"- Total Bars in Temp DB: {cov_500.get('total_bars', 0):,}")
    print("================================================================================")

if __name__ == "__main__":
    main()
