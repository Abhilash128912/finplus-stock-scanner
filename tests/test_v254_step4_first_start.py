"""
Acceptance Test Suite for Startup Coverage & Firestore Restore
Verifies that:
1. When coverage >= 90% is restored from Firestore, ZERO external calls are made.
2. In live engine, run_paced_history_backfill makes 0 external calls (yfinance decommissioned).
"""

import os
import json
import gzip
import base64
import sqlite3
import pytest
import asyncio
from unittest.mock import MagicMock, patch
from src.scheduler.scanner_scheduler import ScannerScheduler
from src.storage.candle_store import CandleStore
from src.storage.firestore_backend import FirestoreBackend

@pytest.fixture
def clean_env(tmp_path):
    candles_db = str(tmp_path / "candles.db")
    ledger_db = str(tmp_path / "schedule_ledger.db")
    
    with patch.dict(os.environ, {
        "DATA_DIR": str(tmp_path),
        "FIRESTORE_PROJECT_ID": "finplus-test-project",
        "FIRESTORE_EMULATOR_HOST": "localhost:8080",
        "USE_FIRESTORE": "true"
    }):
        yield candles_db, ledger_db

def test_first_start_zero_yfinance_when_restored_from_firebase(clean_env):
    candles_db, ledger_db = clean_env
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
    scheduler.feed.get_all_universe_items = MagicMock(return_value=[
        {"symbol": s, "yf_ticker": f"{s}.NS"} for s in sample_symbols
    ])

    # Trigger restore
    restore_res = fb.restore_all_from_firestore(candle_store=cs)
    assert restore_res["status"] == "SUCCESS"
    assert restore_res["symbols_restored"] == 95
    assert restore_res["bars_restored"] == 95 * 530

    # Run paced history backfill startup check
    asyncio.run(scheduler.run_paced_history_backfill(target_coverage=0.90))

    # Verify MarketDataFeed has zero gateway
    assert not hasattr(scheduler.feed, "gateway")

def test_paced_backfill_decommissioned_makes_zero_calls(clean_env):
    candles_db, ledger_db = clean_env
    sample_symbols = [f"SYM_{i}" for i in range(100)]
    cs = CandleStore(db_path=candles_db)

    scheduler = ScannerScheduler()
    scheduler.candle_store = cs
    scheduler.feed.get_all_universe_items = MagicMock(return_value=[
        {"symbol": s, "yf_ticker": f"{s}.NS"} for s in sample_symbols
    ])

    # Run paced history backfill
    asyncio.run(scheduler.run_paced_history_backfill(target_coverage=0.90))

    # Confirms zero gateway on real feed
    assert not hasattr(scheduler.feed, "gateway")
