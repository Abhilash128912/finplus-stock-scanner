"""
Acceptance Test for Item 1: Close Failure Rollback
- Positions removed from memory before INSERT OR IGNORE.
- If insert raises any DB error (e.g. operational error / DB locked), put position back in open list and log ERROR with trade_id.
- Retry close on next poll cycle: succeeds -> 1 row, 1 alert, no alert during failure.
- rowcount == 0 means duplicate: log at DEBUG, no alert.
"""

import gc
import os
import sqlite3
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_SCANNER_DIR = Path(__file__).resolve().parent.parent
if str(_SCANNER_DIR) not in sys.path:
    sys.path.insert(0, str(_SCANNER_DIR))

from src.paper.live_paper_tracker import LivePaperTracker


def test_close_failure_rollback_and_retry(tmp_path):
    db_file = tmp_path / "test_rollback.db"
    tracker = LivePaperTracker(db_path=str(db_file))

    # Add an active position
    pos_id = "test_pos_fail_rollback"
    tracker.active_positions[pos_id] = {
        "position_id": pos_id,
        "symbol": "RELIANCE",
        "sector": "Energy",
        "action": "BUY",
        "is_option": False,
        "entry_price": 2500.0,
        "quantity": 10,
        "initial_sl": 2450.0,
        "current_sl": 2450.0,
        "target": 2600.0,
        "entry_time_ist": "10:00:00 AM IST",
        "opened_at_utc": "2026-11-13T04:30:00Z",
        "status": "OPEN",
    }

    alerts_sent = []
    with patch.object(tracker, "_dispatch_ntfy_trade_closed", side_effect=lambda pos, net_pnl, ep, er: alerts_sent.append(pos["symbol"])):
        # 1. Force DB error on insert (e.g. database locked / disk full)
        original_get_conn = tracker._get_connection
        def mock_error_conn():
            raise sqlite3.OperationalError("database is locked")

        with patch.object(tracker, "_get_connection", side_effect=mock_error_conn):
            res_fail = tracker.close_position(pos_id, 2550.0, "TAKE_PROFIT")

        # Must return False, no alert sent, position rolled back into active_positions
        assert res_fail is False, "Close should return False on DB failure"
        assert len(alerts_sent) == 0, "No alert should be sent on DB failure"
        assert pos_id in tracker.active_positions, "Position must be rolled back into active list"
        assert tracker.active_positions[pos_id]["status"] == "OPEN"

        # 2. Next cycle succeeds: close position with normal DB connection
        res_succ = tracker.close_position(pos_id, 2550.0, "TAKE_PROFIT")
        assert res_succ is True, "Close should succeed on next cycle"
        assert pos_id not in tracker.active_positions, "Position must now be removed from active"
        assert len(alerts_sent) == 1, "Exactly 1 alert should be dispatched"

        # Check DB row count
        with tracker._get_connection() as conn:
            cnt = conn.execute("SELECT COUNT(*) as c FROM closed_paper_trades WHERE position_id = ?", (pos_id,)).fetchone()["c"]
            assert cnt == 1, "Exactly 1 row should exist in closed_paper_trades"

