"""
Tests for First-Start Behavior (Phase 2B / Step 4).
Verifies:
1. When local files are empty and Firebase has candle history for >= 90% of symbols,
   the app restores from Firebase and makes ZERO yfinance calls at startup.
2. The paced backfill only runs for symbols still missing when coverage is below 90%.
"""
import os
import gzip
import json
import base64
import pytest
import sqlite3
import tempfile
import asyncio
from unittest.mock import MagicMock, patch
from src.storage.firestore_backend import FirestoreBackend
from src.storage.candle_store import CandleStore
from src.scheduler.scanner_scheduler import ScannerScheduler


@pytest.fixture
def clean_env():
    fd, candles_db = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    fd2, ledger_db = tempfile.mkstemp(suffix=".db")
    os.close(fd2)
    yield candles_db, ledger_db
    for p in (candles_db, ledger_db):
        try:
            os.remove(p)
        except Exception:
            pass


def test_first_start_zero_yfinance_when_restored_from_firebase(clean_env):
    candles_db, ledger_db = clean_env
    
    # 1. Prepare simulated Firestore documents with 95% universe coverage (95 out of 100 symbols have 530 bars)
    sample_symbols = [f"SYM_{i}" for i in range(100)]
    firestore_docs = []
    sample_bars = [[1731470400 + i * 900, 100.0, 105.0, 99.0, 102.0, 1000] for i in range(530)]
    raw_json = json.dumps(sample_bars)
    b64_payload = base64.b64encode(gzip.compress(raw_json.encode('utf-8'))).decode('ascii')
    
    for s in sample_symbols[:95]:
        mock_doc = MagicMock()
        mock_doc.to_dict.return_value = {
            "symbol": s,
            "timeframe": "15m",
            "bars_count": 530,
            "data_gz_b64": b64_payload
        }
        firestore_docs.append(mock_doc)

    mock_client = MagicMock()
    mock_client.collection.return_value.stream.side_effect = lambda: firestore_docs

    FirestoreBackend._instance = None
    fb = FirestoreBackend.__new__(FirestoreBackend)
    fb._initialized = True
    fb.env_prefix = "test"
    fb.client = mock_client
    fb.is_connected = True
    fb.mode = "CONNECTED (test)"
    fb.writes_today = 0
    fb.reads_today = 0
    fb.deletes_today = 0
    fb.last_restore_result = {}
    fb.last_candle_sync_time = None
    fb.current_ist_date = "2026-11-13"

    cs = CandleStore(db_path=candles_db)
    
    scheduler = ScannerScheduler()
    scheduler.candle_store = cs
    scheduler.firestore = fb
    scheduler.feed = MagicMock()
    scheduler.feed.gateway = MagicMock()
    
    scheduler.feed.get_all_universe_items.return_value = [
        {"symbol": s, "yf_ticker": f"{s}.NS"} for s in sample_symbols
    ]
    scheduler.feed.gateway.download = MagicMock()

    # Trigger restore
    restore_res = fb.restore_all_from_firestore(candle_store=cs)
    assert restore_res["status"] == "SUCCESS"
    assert restore_res["symbols_restored"] == 95
    assert restore_res["bars_restored"] == 95 * 530

    # Run paced history backfill startup check
    asyncio.run(scheduler.run_paced_history_backfill(target_coverage=0.90))

    # Verify ZERO yfinance download calls were made
    assert scheduler.feed.gateway.download.call_count == 0, (
        f"Expected 0 yfinance calls, but download was called {scheduler.feed.gateway.download.call_count} times!"
    )


def test_paced_backfill_runs_only_for_missing_symbols(clean_env):
    candles_db, ledger_db = clean_env
    
    # Only 80 out of 100 symbols restored (80% coverage < 90% threshold)
    sample_symbols = [f"SYM_{i}" for i in range(100)]
    firestore_docs = []
    sample_bars = [[1731470400 + i * 900, 100.0, 105.0, 99.0, 102.0, 1000] for i in range(530)]
    raw_json = json.dumps(sample_bars)
    b64_payload = base64.b64encode(gzip.compress(raw_json.encode('utf-8'))).decode('ascii')
    
    for s in sample_symbols[:80]:
        mock_doc = MagicMock()
        mock_doc.to_dict.return_value = {
            "symbol": s,
            "timeframe": "15m",
            "bars_count": 530,
            "data_gz_b64": b64_payload
        }
        firestore_docs.append(mock_doc)

    mock_client = MagicMock()
    mock_client.collection.return_value.stream.side_effect = lambda: firestore_docs

    FirestoreBackend._instance = None
    fb = FirestoreBackend.__new__(FirestoreBackend)
    fb._initialized = True
    fb.env_prefix = "test"
    fb.client = mock_client
    fb.is_connected = True
    fb.mode = "CONNECTED (test)"
    fb.writes_today = 0
    fb.reads_today = 0
    fb.deletes_today = 0
    fb.last_restore_result = {}
    fb.last_candle_sync_time = None
    fb.current_ist_date = "2026-11-13"

    cs = CandleStore(db_path=candles_db)
    
    scheduler = ScannerScheduler()
    scheduler.candle_store = cs
    scheduler.firestore = fb
    scheduler.is_running = True
    scheduler.feed = MagicMock()
    scheduler.feed.gateway = MagicMock()
    scheduler.feed.get_all_universe_items.return_value = [
        {"symbol": s, "yf_ticker": f"{s}.NS"} for s in sample_symbols
    ]
    scheduler.feed.gateway.download = MagicMock(return_value=None)

    # Restore from Firestore (80 symbols)
    fb.restore_all_from_firestore(candle_store=cs)

    # Run paced history backfill
    asyncio.run(scheduler.run_paced_history_backfill(target_coverage=0.90, chunk_size=50, pause_seconds=0.01))

    # Verify download was called ONLY for the 20 missing symbols (SYM_80 to SYM_99)
    assert scheduler.feed.gateway.download.call_count == 1
    call_args, call_kwargs = scheduler.feed.gateway.download.call_args
    downloaded_tickers = call_args[0]
    expected_missing = [f"SYM_{i}.NS" for i in range(80, 100)]
    assert sorted(downloaded_tickers) == sorted(expected_missing)
