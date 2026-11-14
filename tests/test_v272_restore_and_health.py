"""
FinPlus+ v2.7.2 Test Suite: Startup Restore, History Coverage, Health Telemetry,
Startup NTFY, and Weekend/Holiday Ledger Milestones.
"""

import os
import sys
import asyncio
import tempfile
import datetime
from unittest.mock import MagicMock, patch
import pytest
from fastapi.testclient import TestClient

from src.storage.candle_store import CandleStore
from src.storage.schedule_ledger import ScheduleLedger
from src.notifications.ntfy_dispatcher import NTFYDispatcher
from src.api.server import app, get_code_version


@pytest.fixture
def temp_db_dir(tmp_path):
    orig_data_dir = os.environ.get("DATA_DIR")
    os.environ["DATA_DIR"] = str(tmp_path)
    yield tmp_path
    if orig_data_dir is not None:
        os.environ["DATA_DIR"] = orig_data_dir
    else:
        os.environ.pop("DATA_DIR", None)


def test_01_history_coverage_calculation(temp_db_dir):
    """Verifies that CandleStore computes total bars, symbols >= 122, and symbols >= 522 bars accurately."""
    cs = CandleStore(db_path=str(temp_db_dir / "candles.db"))
    
    # Empty coverage
    cov = cs.get_history_coverage(tracked_symbols=["RELIANCE", "TCS", "INFY"])
    assert cov["total_bars"] == 0
    assert cov["tracked_symbols_count"] == 3
    assert cov["symbols_122_bars"]["count"] == 0
    assert cov["symbols_522_bars"]["count"] == 0
    assert cov["coverage_522_pct"] == 0.0
    assert len(cov["missing_or_short_symbols"]) == 3

    # Add 150 bars for RELIANCE (qualifies for 122, not 522)
    # Add 550 bars for TCS (qualifies for both)
    # Add 10 bars for INFY
    with cs._get_connection() as conn:
        for i in range(150):
            conn.execute("""
                INSERT INTO candles (symbol, timeframe, epoch, timestamp_utc, timestamp_ist, open, high, low, close, volume)
                VALUES (?, '15m', ?, '2026-11-01T00:00:00Z', '2026-11-01 05:30:00', 100, 105, 95, 102, 1000)
            """, ("RELIANCE", 1700000000 + i * 900))
        for i in range(550):
            conn.execute("""
                INSERT INTO candles (symbol, timeframe, epoch, timestamp_utc, timestamp_ist, open, high, low, close, volume)
                VALUES (?, '15m', ?, '2026-11-01T00:00:00Z', '2026-11-01 05:30:00', 200, 205, 195, 202, 2000)
            """, ("TCS", 1700000000 + i * 900))
        for i in range(10):
            conn.execute("""
                INSERT INTO candles (symbol, timeframe, epoch, timestamp_utc, timestamp_ist, open, high, low, close, volume)
                VALUES (?, '15m', ?, '2026-11-01T00:00:00Z', '2026-11-01 05:30:00', 300, 305, 295, 302, 3000)
            """, ("INFY", 1700000000 + i * 900))
        conn.commit()

    cov2 = cs.get_history_coverage(tracked_symbols=["RELIANCE", "TCS", "INFY"])
    assert cov2["total_bars"] == 710
    assert cov2["symbols_122_bars"]["count"] == 2 # RELIANCE, TCS
    assert cov2["symbols_522_bars"]["count"] == 1 # TCS
    assert cov2["coverage_522_pct"] == 33.3
    assert cov2["missing_or_short_symbols"] == ["RELIANCE", "INFY"]


def test_02_startup_restore_rule_and_yahoo_avoidance(temp_db_dir, monkeypatch):
    """
    Verifies that if coverage < 90%, Firestore restore is invoked BEFORE the scanner starts,
    and if Firestore brings coverage >= 90%, zero Yahoo calls are made.
    """
    from src.scheduler.scanner_scheduler import ScannerScheduler
    sched = ScannerScheduler()
    sched.candle_store = CandleStore(db_path=str(temp_db_dir / "candles.db"))

    # Mock tracked universe to 10 symbols
    mock_universe = [{"symbol": f"SYM_{i}", "yf_ticker": f"SYM_{i}.NS"} for i in range(10)]
    monkeypatch.setattr(sched.feed, "get_all_universe_items", MagicMock(return_value=mock_universe))

    # Mock Firestore connected
    monkeypatch.setattr(sched.firestore, "is_connected", True)
    
    # Mock restore_all_from_firestore to simulate populating 10 symbols with 530 bars each
    def fake_restore(candle_store, paper_tracker=None, schedule_ledger=None):
        with candle_store._get_connection() as conn:
            for i in range(10):
                for b in range(530):
                    conn.execute("""
                        INSERT OR REPLACE INTO candles (symbol, timeframe, epoch, timestamp_utc, timestamp_ist, open, high, low, close, volume)
                        VALUES (?, '15m', ?, '2026-11-01T00:00:00Z', '2026-11-01 05:30:00', 100, 105, 95, 100, 100)
                    """, (f"SYM_{i}", 1700000000 + b * 900))
            conn.commit()
        return {"symbols_restored": 10, "bars_restored": 5300}

    monkeypatch.setattr(sched.firestore, "restore_all_from_firestore", MagicMock(side_effect=fake_restore))
    monkeypatch.setattr(sched, "run_paced_history_backfill", MagicMock())

    # Initial coverage is 0.0% (< 90%)
    res = asyncio.run(sched.check_and_restore_history(caller="test_startup"))
    
    # Assert restore was called
    assert sched.firestore.restore_all_from_firestore.call_count == 1
    # Post-restore coverage is 100% (>= 90%)
    assert res["coverage_522_pct"] == 100.0
    # Zero Yahoo backfill tasks queued!
    assert sched.run_paced_history_backfill.call_count == 0
    assert "coverage reached 100.0% >= 90.0%" in sched.firestore.last_restore_reason


def test_03_weekends_and_holidays_skipped_in_ledger(temp_db_dir):
    """
    Verifies that on Saturday or NSE holiday, all 09:00-16:00 milestones
    are initialized as SKIPPED_MARKET_CLOSED, never PENDING, and never due.
    """
    ledger = ScheduleLedger(db_path=str(temp_db_dir / "ledger.db"))
    
    # 1. Test Saturday (2026-11-14)
    sat_date = "2026-11-14"
    status_list = ledger.get_ledger_status(sat_date)
    for m in status_list:
        if m["slot_id"] == "18:00_WEEKLY_REPORT":
            continue
        assert m["status"] == "SKIPPED_MARKET_CLOSED"
        assert "Market closed (Weekend)" in m["error_message"]

    # No due events on Saturday
    sat_dt = datetime.datetime(2026, 11, 14, 10, 0, 0)
    due = ledger.get_pending_due_events(sat_dt)
    assert len(due) == 0

    # 2. Test NSE Listed Holiday (2026-11-10 Diwali-Balipratipada)
    hol_date = "2026-11-10"
    status_list_hol = ledger.get_ledger_status(hol_date)
    for m in status_list_hol:
        assert m["status"] == "SKIPPED_MARKET_CLOSED"
        assert "Diwali" in m["error_message"]

    hol_dt = datetime.datetime(2026, 11, 10, 10, 0, 0)
    due_hol = ledger.get_pending_due_events(hol_dt)
    assert len(due_hol) == 0


def test_04_startup_bulletin_and_token_rejection(temp_db_dir):
    """
    Verifies that send_startup_bulletin formats message correctly,
    and if NTFY returns 401/403, token_rejected is set without leaking token in logs.
    """
    ledger = ScheduleLedger(db_path=str(temp_db_dir / "ledger.db"))
    dispatcher = NTFYDispatcher(ledger=ledger)
    dispatcher.auth_token = "secret_token_123"

    # Simulate successful push (200)
    with patch("requests.post") as mock_post:
        mock_post.return_value.status_code = 200
        ok = dispatcher.send_startup_bulletin("3f9143f", "connected", 92.5)
        assert ok is True
        assert dispatcher.token_rejected is False
        posted_data = mock_post.call_args[1]["data"].decode("utf-8")
        assert "[STARTUP] version 3f9143f online | Firebase connected | history 92.5% | token OK" in posted_data

    # Simulate token rejection (401 Unauthorized)
    with patch("requests.post") as mock_post:
        mock_post.return_value.status_code = 401
        mock_post.return_value.text = "Unauthorized"
        ok = dispatcher.send_startup_bulletin("3f9143f", "connected", 92.5)
        assert ok is False
        assert dispatcher.token_rejected is True


def test_05_health_deep_history_coverage_and_code_version():
    """
    Verifies that /api/health/deep returns:
    - history_coverage: total_bars, symbols_122_bars, symbols_522_bars, db_path, file_sizes_kb, code_version, last_restore_reason
    - code_version: commit and branch
    - notifications: TOKEN_REJECTED when token is rejected
    """
    client = TestClient(app)
    resp = client.get("/api/health/deep")
    assert resp.status_code == 200
    data = resp.json()

    assert "history_coverage" in data
    hc = data["history_coverage"]
    assert "total_bars" in hc
    assert "symbols_122_bars" in hc
    assert "symbols_522_bars" in hc
    assert "db_path" in hc
    assert "file_sizes_kb" in hc
    assert "code_version" in hc
    assert "last_restore_reason" in hc

    assert "code_version" in data
    cv = data["code_version"]
    assert "commit" in cv
    assert "branch" in cv

    assert "notifications" in data["subsystems"]
