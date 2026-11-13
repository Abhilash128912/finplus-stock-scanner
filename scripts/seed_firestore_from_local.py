"""
FinPlus+ Seed Firestore from Local SQLite (Phase 2B / Step 2)
============================================================
Reads local data/candles.db and uploads ONE document per symbol (compact gzipped format)
under the live prefix 'env/live/candles' in Firestore.

Usage:
    python scripts/seed_firestore_from_local.py --key-file /path/to/creds.json [--dry-run]
    python scripts/seed_firestore_from_local.py --key-file /path/to/creds.json --yes

Safety Rules:
- Refuses to run if --key-file is inside the project folder or any git repository.
- Key file is read strictly in memory; never copied, saved, logged, or committed.
- Candle history ONLY: no paper trades, ledger or stats are uploaded.
- Verifies integrity after upload by reading back 20 random symbols and comparing bars.
"""

import os
import sys
import json
import gzip
import base64
import random
import sqlite3
import argparse
import subprocess
from pathlib import Path
from typing import List, Dict, Any, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("DATA_DIR", REPO_ROOT / "data"))
CANDLES_DB = DATA_DIR / "candles.db"


def is_inside_git_repo(path: Path) -> bool:
    """Checks whether a given path is inside any git working tree."""
    try:
        dir_to_check = path if path.is_dir() else path.parent
        res = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=str(dir_to_check),
            capture_output=True,
            text=True
        )
        return res.returncode == 0 and res.stdout.strip().lower() == "true"
    except Exception:
        return False


def validate_key_file(key_path_str: str) -> Path:
    """Validates key file existence and enforces that it is strictly outside project and git repos."""
    key_path = Path(key_path_str).resolve()
    if not key_path.exists():
        print(f"[-] ERROR: Key file not found: {key_path}", file=sys.stderr)
        sys.exit(1)

    # 1. Reject if inside project folder
    try:
        if key_path.is_relative_to(REPO_ROOT.resolve()):
            print("[-] SECURITY REFUSAL: The service account key file is INSIDE the project repository directory!", file=sys.stderr)
            print("    Store your credential file outside the repo (e.g. in your user home or downloads) to prevent accidental commit.", file=sys.stderr)
            sys.exit(1)
    except AttributeError:
        # Python < 3.9 fallback
        if str(key_path).startswith(str(REPO_ROOT.resolve())):
            print("[-] SECURITY REFUSAL: The service account key file is INSIDE the project repository directory!", file=sys.stderr)
            sys.exit(1)

    # 2. Reject if inside ANY git repository
    if is_inside_git_repo(key_path):
        print("[-] SECURITY REFUSAL: The service account key file is inside a git repository!", file=sys.stderr)
        print("    Move the key file to an unversioned folder outside any git tracking.", file=sys.stderr)
        sys.exit(1)

    return key_path


def get_all_symbols_from_db(db_path: Path, timeframe: str = "15m") -> List[str]:
    if not db_path.exists():
        print(f"[-] ERROR: SQLite database not found at {db_path}", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT symbol FROM candles WHERE timeframe = ? ORDER BY symbol ASC", (timeframe,))
    symbols = [r[0] for r in cursor.fetchall()]
    conn.close()
    return symbols


def get_symbol_candles(db_path: Path, symbol: str, timeframe: str = "15m") -> List[Tuple[int, float, float, float, float, int]]:
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()
    cursor.execute("""
        SELECT epoch, open, high, low, close, volume 
        FROM candles 
        WHERE symbol = ? AND timeframe = ? 
        ORDER BY epoch ASC
    """, (symbol, timeframe))
    bars = [(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), int(r[5])) for r in cursor.fetchall()]
    conn.close()
    return bars


def compress_bars_matrix(bars: List[Tuple[int, float, float, float, float, int]]) -> Tuple[str, int, int]:
    matrix = [list(b) for b in bars]
    raw_json = json.dumps(matrix, separators=(',', ':'))
    raw_bytes = raw_json.encode('utf-8')
    compressed = gzip.compress(raw_bytes)
    b64_str = base64.b64encode(compressed).decode('ascii')
    return b64_str, len(raw_bytes), len(compressed)


def decompress_bars_matrix(b64_str: str) -> List[Tuple[int, float, float, float, float, int]]:
    compressed = base64.b64decode(b64_str.encode('ascii'))
    raw_json = gzip.decompress(compressed).decode('utf-8')
    matrix = json.loads(raw_json)
    return [(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), int(r[5])) for r in matrix]


def main():
    parser = argparse.ArgumentParser(description="Seed Firestore with candle history from local SQLite database.")
    parser.add_argument("--key-file", required=True, help="Path to the Firebase service-account JSON file on laptop.")
    parser.add_argument("--yes", action="store_true", help="Perform the actual upload to Firestore (default is dry-run).")
    parser.add_argument("--dry-run", action="store_true", default=False, help="Perform dry-run calculations without uploading.")
    parser.add_argument("--timeframe", default="15m", help="Candle timeframe (default: 15m).")
    args = parser.parse_args()

    is_dry_run = not args.yes

    print("\n=======================================================")
    print(" FinPlus+ Firestore Candle Seeder (Phase 2B Step 2)")
    print("=======================================================")

    # 1. Security Check
    key_path = validate_key_file(args.key_file)
    print(f"[+] Security check passed: Key file is external to repo: {key_path}")

    # 2. Inspect SQLite
    symbols = get_all_symbols_from_db(CANDLES_DB, timeframe=args.timeframe)
    total_symbols = len(symbols)
    print(f"[+] Found {total_symbols} unique symbols in local SQLite '{CANDLES_DB.name}' (timeframe: {args.timeframe}).")

    if total_symbols == 0:
        print("[-] No symbols found in database. Exiting.")
        sys.exit(0)

    # 3. Analyze payload and compression
    total_bars = 0
    total_uncompressed = 0
    total_compressed = 0
    symbol_data = {}

    for s in symbols:
        bars = get_symbol_candles(CANDLES_DB, s, timeframe=args.timeframe)
        if not bars:
            continue
        b64_payload, raw_len, comp_len = compress_bars_matrix(bars)
        total_bars += len(bars)
        total_uncompressed += raw_len
        total_compressed += comp_len
        symbol_data[s] = {
            "bars": bars,
            "b64": b64_payload,
            "raw_len": raw_len,
            "comp_len": comp_len
        }

    valid_symbols = list(symbol_data.keys())
    avg_comp_kb = (total_compressed / len(valid_symbols) / 1024.0) if valid_symbols else 0.0
    total_comp_mb = total_compressed / (1024.0 * 1024.0)
    total_raw_mb = total_uncompressed / (1024.0 * 1024.0)

    print("\n--- Payload Telemetry ---")
    print(f"Total Symbols with Bars : {len(valid_symbols)} / {total_symbols}")
    print(f"Total 15m Bars          : {total_bars:,}")
    print(f"Uncompressed JSON Size  : {total_raw_mb:.2f} MB")
    print(f"Compressed Payload Size : {total_comp_mb:.2f} MB (Average: {avg_comp_kb:.2f} KB / symbol)")
    print(f"Firestore Writes Needed : {len(valid_symbols)} (out of 20,000 daily free Spark quota: {len(valid_symbols)/20000*100:.1f}%)")
    print(f"Target Collection       : env/live/candles")

    if is_dry_run:
        print("\n[!] DRY-RUN MODE: No writes were made to Firestore.")
        print("    To execute the live upload, re-run with: --yes")
        sys.exit(0)

    # 4. Initialize Firebase Admin
    print("\n[+] Initializing Firebase Admin SDK...")
    try:
        import firebase_admin
        from firebase_admin import credentials, firestore
        cred = credentials.Certificate(str(key_path))
        try:
            app = firebase_admin.get_app("seed_worker")
        except ValueError:
            app = firebase_admin.initialize_app(cred, name="seed_worker")
        db = firestore.client(app=app)
    except Exception as e:
        print(f"[-] Failed to initialize Firebase client: {e}", file=sys.stderr)
        sys.exit(1)

    # 5. Upload Symbol Documents
    print(f"[+] Starting live upload to 'env/live/candles' ({len(valid_symbols)} symbols)...")
    writes_count = 0
    errors = []

    for idx, s in enumerate(valid_symbols, 1):
        info = symbol_data[s]
        doc_ref = db.collection("env/live/candles").document(f"{s}_{args.timeframe}")
        doc_payload = {
            "symbol": s,
            "timeframe": args.timeframe,
            "bars_count": len(info["bars"]),
            "uncompressed_bytes": info["raw_len"],
            "compressed_bytes": info["comp_len"],
            "data_gz_b64": info["b64"]
        }
        try:
            doc_ref.set(doc_payload)
            writes_count += 1
            if idx % 50 == 0 or idx == len(valid_symbols):
                print(f"    Uploaded {idx}/{len(valid_symbols)} symbols ({writes_count} writes)...")
        except Exception as upload_err:
            err_str = str(upload_err)
            errors.append((s, err_str))
            print(f"[-] Error uploading {s}: {upload_err}", file=sys.stderr)
            if "Cloud Firestore API has not been used" in err_str or "API has not been enabled" in err_str or "403" in err_str:
                print("\n[-] FATAL ERROR: Cloud Firestore Database is not initialized or API is disabled in this Google Cloud/Firebase project!", file=sys.stderr)
                print("    1. Go to Firebase Console (https://console.firebase.google.com/project/finplus-stock-scanner/firestore)", file=sys.stderr)
                print("    2. Click 'Create database' (choose Production mode, and select your default location, e.g. asia-south1 or nam5)", file=sys.stderr)
                print("    3. Or enable the API at https://console.developers.google.com/apis/api/firestore.googleapis.com/overview?project=finplus-stock-scanner", file=sys.stderr)
                print("    Then retry running this script.", file=sys.stderr)
                sys.exit(1)

    print(f"\n[+] Upload Complete: {writes_count} writes performed, {len(errors)} errors.")
    if errors:
        print(f"[-] Encountered {len(errors)} errors during upload:")
        for err_sym, err_msg in errors[:5]:
            print(f"    - {err_sym}: {err_msg}")

    # 6. Verification: Sample 20 random symbols, read back, and compare
    verify_sample_size = min(20, len(valid_symbols))
    sample_symbols = random.sample(valid_symbols, verify_sample_size)
    print(f"\n[+] Verifying integrity: Reading back and comparing {verify_sample_size} random symbols...")

    verified_count = 0
    for s in sample_symbols:
        doc_ref = db.collection("env/live/candles").document(f"{s}_{args.timeframe}")
        doc = doc_ref.get()
        if not doc.exists:
            print(f"[-] VERIFICATION FAILED: Document for {s} does not exist in Firestore!")
            continue

        remote_data = doc.to_dict()
        remote_b64 = remote_data.get("data_gz_b64")
        if not remote_b64:
            print(f"[-] VERIFICATION FAILED: {s} has empty payload_b64!")
            continue

        decompressed_bars = decompress_bars_matrix(remote_b64)
        local_bars = symbol_data[s]["bars"]

        if len(decompressed_bars) != len(local_bars):
            print(f"[-] VERIFICATION FAILED: {s} bar count mismatch! Local: {len(local_bars)}, Remote: {len(decompressed_bars)}")
            continue

        # Exact match comparison
        if decompressed_bars != local_bars:
            print(f"[-] VERIFICATION FAILED: {s} candle values did not match exactly!")
            continue

        verified_count += 1

    print(f"[+] VERIFICATION SUCCESS: {verified_count}/{verify_sample_size} random symbols matched identically row-for-row.")
    print("=======================================================\n")


if __name__ == "__main__":
    main()
