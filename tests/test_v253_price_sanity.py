"""
Acceptance Test Suite for FinPlus+ Stock Scanner (APP-02) v2.5.3:
Price Sanity Guard Hardening & Risk Mitigations

Items Tested:
1. RESET REFERENCE AFTER GAP (>30s gap accepts tick as new reference with no % check)
2. NEVER BLOCK EXITS (Stop-loss, target, and 15:00 / MCX cutoff exits succeed even if tick outside sanity band; WARNING logged)
3. SEPARATE LIMITS (Cash default 15%, Option default 40%, MCX default 10%)
4. DATA_SUSPECT AUTO-CLEAR (3 bad ticks -> flagged; 3 steady ticks within band -> cleared; only blocks new entries)
5. REJECTED-TICK COUNTER (Count rejected ticks per symbol per day, included in /api/health/deep)
"""

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_SCANNER_DIR = Path(__file__).resolve().parent.parent
if str(_SCANNER_DIR) not in sys.path:
    sys.path.insert(0, str(_SCANNER_DIR))

from src.paper.live_paper_tracker import LivePaperTracker


def test_01_reset_reference_after_gap(tmp_path):
    """TEST 1: last tick 45s ago, new tick +20% -> accepted, reference updated."""
    db_file = tmp_path / "test_gap.db"
    tracker = LivePaperTracker(db_path=str(db_file))
    tracker.price_sanity_pct_cash = 15.0

    pos_id = "pos_gap_test"
    tracker.active_positions[pos_id] = {
        "position_id": pos_id,
        "symbol": "GAPSYM",
        "sector": "INTRADAY_CASH",
        "action": "BUY",
        "is_option": False,
        "entry_price": 100.0,
        "current_price": 100.0,
        "quantity": 10,
        "risk_r": 2.0,
        "initial_sl": 90.0,
        "trailing_sl": 90.0,
        "target": 150.0,
        "entry_time_ist": "10:00:00 AM IST",
        "opened_at_utc": "2026-11-13T04:30:00Z",
        "status": "OPEN",
        "stage": "INITIAL (FIXED SL)",
        "unrealized_pnl": 0.0,
    }

    # Simulate last accepted tick recorded 45 seconds ago
    tracker.last_accepted_tick_time["GAPSYM"] = time.time() - 45.0

    # Inject +20% tick (120.0). Normally >15% is rejected, but because gap > 30s, accepted!
    with patch.object(tracker, "_fetch_live_price", return_value=120.0):
        with patch.object(tracker.indmoney_feed, "fetch_batch_quotes"):
            tracker.update_and_trail_positions()

    pos = tracker.active_positions[pos_id]
    assert pos["current_price"] == 120.0, "Tick was not accepted despite >30s feed gap!"
    assert pos["unrealized_pnl"] == 200.0, "P&L was not updated after gap reset!"
    assert tracker.rejected_ticks_count.get("GAPSYM", 0) == 0, "Rejected count should remain 0 on gap reset!"


def test_02_never_block_exits_stop_loss_and_squareoff(tmp_path):
    """TEST 2: +/-30% tick at stop-loss or square-off -> position closes, WARNING logged."""
    db_file = tmp_path / "test_exit.db"
    tracker = LivePaperTracker(db_path=str(db_file))
    tracker.price_sanity_pct_cash = 15.0

    # Case A: Real Crash through Stop-Loss (-30% drop: 100 -> 70, SL is 95)
    pos_sl = "pos_sl_crash"
    tracker.active_positions[pos_sl] = {
        "position_id": pos_sl,
        "symbol": "CRASHSYM",
        "sector": "INTRADAY_CASH",
        "action": "BUY",
        "is_option": False,
        "entry_price": 100.0,
        "current_price": 100.0,
        "quantity": 10,
        "risk_r": 5.0,
        "initial_sl": 95.0,
        "trailing_sl": 95.0,
        "target": 120.0,
        "entry_time_ist": "10:00:00 AM IST",
        "opened_at_utc": "2026-11-13T04:30:00Z",
        "status": "OPEN",
        "stage": "INITIAL (FIXED SL)",
        "unrealized_pnl": 0.0,
    }
    tracker.last_accepted_tick_time["CRASHSYM"] = time.time()  # recent tick

    # Inject 70.0 (-30% jump) -> must close position despite being outside sanity band!
    with patch.object(tracker, "_fetch_live_price", return_value=70.0):
        with patch.object(tracker.indmoney_feed, "fetch_batch_quotes"):
            tracker.update_and_trail_positions()

    assert pos_sl not in tracker.active_positions, "Position failed to close on real crash through stop-loss!"
    with tracker._get_connection() as conn:
        closed_row = conn.execute("SELECT * FROM closed_paper_trades WHERE position_id = ?", (pos_sl,)).fetchone()
        assert closed_row is not None, "Closed trade row missing from SQLite database"
        assert closed_row["exit_reason"] == "INITIAL_SL_HIT"

    # Case B: 15:00 Square-Off with wide tick
    pos_eod = "pos_eod_squareoff"
    tracker.active_positions[pos_eod] = {
        "position_id": pos_eod,
        "symbol": "EODSYM",
        "sector": "INTRADAY_CASH",
        "action": "BUY",
        "is_option": False,
        "entry_price": 100.0,
        "current_price": 100.0,
        "quantity": 10,
        "risk_r": 5.0,
        "initial_sl": 80.0,
        "trailing_sl": 80.0,
        "target": 130.0,
        "entry_time_ist": "10:00:00 AM IST",
        "opened_at_utc": "2026-11-13T04:30:00Z",
        "status": "OPEN",
        "stage": "INITIAL (FIXED SL)",
        "unrealized_pnl": 0.0,
    }
    tracker.last_accepted_tick_time["EODSYM"] = time.time()

    # Simulate 15:05 IST (after 15:00 EOD cutoff)
    with patch("src.paper.live_paper_tracker.datetime") as mock_dt:
        from datetime import datetime, timezone, timedelta
        mock_ist_now = datetime(2026, 11, 13, 15, 5, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
        mock_dt.datetime.now.return_value = mock_ist_now
        mock_dt.timezone = timezone
        mock_dt.timedelta = timedelta

        # Inject +30% tick at 15:00 square-off
        with patch.object(tracker, "_fetch_live_price", return_value=130.0):
            with patch.object(tracker.indmoney_feed, "fetch_batch_quotes"):
                tracker.update_and_trail_positions()

    assert pos_eod not in tracker.active_positions, "Position failed to square off at 15:00 IST!"


def test_03_separate_limits_cash_vs_option(tmp_path):
    """TEST 3: +25% option tick -> accepted. +25% cash tick -> rejected."""
    db_file = tmp_path / "test_limits.db"
    tracker = LivePaperTracker(db_path=str(db_file))
    tracker.price_sanity_pct_cash = 15.0
    tracker.price_sanity_pct_option = 40.0
    now_ts = time.time()

    # Cash position: 100.0 entry
    pos_cash = "pos_cash_limit"
    tracker.active_positions[pos_cash] = {
        "position_id": pos_cash,
        "symbol": "CASHLIM",
        "sector": "INTRADAY_CASH",
        "action": "BUY",
        "is_option": False,
        "entry_price": 100.0,
        "current_price": 100.0,
        "quantity": 10,
        "risk_r": 2.0,
        "initial_sl": 90.0,
        "trailing_sl": 90.0,
        "target": 200.0,
        "entry_time_ist": "10:00:00 AM IST",
        "opened_at_utc": "2026-11-13T04:30:00Z",
        "status": "OPEN",
        "stage": "INITIAL (FIXED SL)",
        "unrealized_pnl": 0.0,
    }
    tracker.last_accepted_tick_time["CASHLIM"] = now_ts

    # Option position: 100.0 entry
    pos_opt = "pos_opt_limit"
    tracker.active_positions[pos_opt] = {
        "position_id": pos_opt,
        "symbol": "OPTLIM 100 CE",
        "sector": "STOCK_OPTIONS",
        "action": "BUY",
        "is_option": True,
        "entry_price": 100.0,
        "current_price": 100.0,
        "quantity": 100,
        "risk_r": 2.0,
        "initial_sl": 90.0,
        "trailing_sl": 90.0,
        "target": 200.0,
        "opt_entry_premium": 10.0,
        "opt_current_premium": 10.0,
        "opt_initial_sl": 7.0,
        "opt_trailing_sl": 7.0,
        "opt_target": 50.0,
        "entry_time_ist": "10:00:00 AM IST",
        "opened_at_utc": "2026-11-13T04:30:00Z",
        "status": "OPEN",
        "stage": "INITIAL (FIXED SL)",
        "unrealized_pnl": 0.0,
    }
    tracker.last_accepted_tick_time["OPTLIM 100 CE"] = now_ts

    # Inject +25% tick (125.0) for both
    # 1. Cash: 25% > 15% -> REJECTED
    with patch.object(tracker, "_fetch_live_price", return_value=125.0):
        with patch.object(tracker.indmoney_feed, "fetch_batch_quotes"):
            tracker.update_and_trail_positions()

    assert tracker.active_positions[pos_cash]["current_price"] == 100.0, "Cash position incorrectly accepted +25% tick!"
    assert tracker.rejected_ticks_count.get("CASHLIM") == 1

    # 2. Option: 25% <= 40% -> ACCEPTED
    assert tracker.active_positions[pos_opt]["current_price"] == 125.0, "Option position incorrectly rejected +25% tick (within 40% band)!"


def test_04_data_suspect_auto_clear(tmp_path):
    """TEST 4: 3 bad ticks -> flagged. 3 steady ticks -> cleared. Only block new entries, never exits."""
    db_file = tmp_path / "test_suspect.db"
    tracker = LivePaperTracker(db_path=str(db_file))
    tracker.price_sanity_pct_cash = 15.0
    now_ts = time.time()

    pos_id = "pos_suspect_test"
    tracker.active_positions[pos_id] = {
        "position_id": pos_id,
        "symbol": "FLAPSYM",
        "sector": "INTRADAY_CASH",
        "action": "BUY",
        "is_option": False,
        "entry_price": 100.0,
        "current_price": 100.0,
        "quantity": 10,
        "risk_r": 2.0,
        "initial_sl": 90.0,
        "trailing_sl": 90.0,
        "target": 150.0,
        "entry_time_ist": "10:00:00 AM IST",
        "opened_at_utc": "2026-11-13T04:30:00Z",
        "status": "OPEN",
        "stage": "INITIAL (FIXED SL)",
        "unrealized_pnl": 0.0,
    }
    tracker.last_accepted_tick_time["FLAPSYM"] = now_ts

    # 1. Inject 3 consecutive bad ticks (120.0, 125.0, 130.0 -> all >15% jump from 100)
    for bad_tick in [120.0, 125.0, 130.0]:
        with patch.object(tracker, "_fetch_live_price", return_value=bad_tick):
            with patch.object(tracker.indmoney_feed, "fetch_batch_quotes"):
                tracker.update_and_trail_positions()

    assert tracker.rejected_ticks_count.get("FLAPSYM") == 3
    assert "FLAPSYM" in tracker.suspect_symbols, "FLAPSYM was not flagged DATA_SUSPECT after 3 bad ticks!"

    # 2. Verify NEW entries are blocked for DATA_SUSPECT
    dummy_shortlist = {
        "best_intraday_stock": {
            "symbol": "FLAPSYM",
            "confluence_score": 90,
            "regime_confluence": "APPROVED",
            "last_price": 100.0,
            "intraday_action_badge": "BUY"
        }
    }
    initial_open_count = len(tracker.active_positions)
    tracker.ingest_shortlist(dummy_shortlist)
    assert len(tracker.active_positions) == initial_open_count, "New entry was allowed for DATA_SUSPECT symbol!"

    # 3. Inject 3 consecutive steady ticks within band (101.0, 102.0, 101.5)
    for steady_tick in [101.0, 102.0, 101.5]:
        with patch.object(tracker, "_fetch_live_price", return_value=steady_tick):
            with patch.object(tracker.indmoney_feed, "fetch_batch_quotes"):
                tracker.update_and_trail_positions()

    # Symbol must now be AUTO-CLEARED from suspect_symbols
    assert "FLAPSYM" not in tracker.suspect_symbols, "FLAPSYM was not auto-cleared after 3 steady ticks within band!"


def test_05_rejected_tick_counter_telemetry(tmp_path):
    """TEST 5: 5 injected rejects -> counter shows 5."""
    db_file = tmp_path / "test_counter.db"
    tracker = LivePaperTracker(db_path=str(db_file))
    tracker.price_sanity_pct_cash = 15.0
    now_ts = time.time()

    pos_id = "pos_count_test"
    tracker.active_positions[pos_id] = {
        "position_id": pos_id,
        "symbol": "COUNTME",
        "sector": "INTRADAY_CASH",
        "action": "BUY",
        "is_option": False,
        "entry_price": 100.0,
        "current_price": 100.0,
        "quantity": 10,
        "risk_r": 2.0,
        "initial_sl": 80.0,
        "trailing_sl": 80.0,
        "target": 200.0,
        "entry_time_ist": "10:00:00 AM IST",
        "opened_at_utc": "2026-11-13T04:30:00Z",
        "status": "OPEN",
        "stage": "INITIAL (FIXED SL)",
        "unrealized_pnl": 0.0,
    }
    tracker.last_accepted_tick_time["COUNTME"] = now_ts

    # Inject 5 bad ticks
    for _ in range(5):
        with patch.object(tracker, "_fetch_live_price", return_value=125.0):
            with patch.object(tracker.indmoney_feed, "fetch_batch_quotes"):
                tracker.update_and_trail_positions()

    telemetry = tracker.get_rejected_ticks_telemetry()
    assert telemetry["total_rejected_ticks"] == 5, f"Expected total 5 rejected ticks, got {telemetry['total_rejected_ticks']}"
    assert telemetry["per_symbol"].get("COUNTME") == 5, f"Expected COUNTME to have 5 rejections, got {telemetry['per_symbol'].get('COUNTME')}"
