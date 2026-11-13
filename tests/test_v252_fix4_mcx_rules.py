"""
Acceptance Test for Item 4: Commodity (MCX) Session Rules
- 15:00 IST square-off does NOT close MCX positions (e.g. GOLDPETAL).
- MCX cutoff is configurable (default 23:15 IST).
- At 15:00 IST: equity position closes, MCX position stays open.
"""

import datetime
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_SCANNER_DIR = Path(__file__).resolve().parent.parent
if str(_SCANNER_DIR) not in sys.path:
    sys.path.insert(0, str(_SCANNER_DIR))

from src.paper.live_paper_tracker import LivePaperTracker, IST


def test_mcx_session_rules_at_1500_ist(tmp_path):
    db_file = tmp_path / "test_mcx_rules.db"
    tracker = LivePaperTracker(db_path=str(db_file))

    # Add 1 Equity position and 1 MCX Commodity position
    eq_id = "pos_eq_tcs"
    mcx_id = "pos_mcx_gold"

    tracker.active_positions[eq_id] = {
        "position_id": eq_id,
        "symbol": "TCS",
        "sector": "INTRADAY_CASH",
        "action": "BUY",
        "is_option": False,
        "entry_price": 3500.0,
        "current_price": 3510.0,
        "quantity": 10,
        "risk_r": 20.0,
        "initial_sl": 3480.0,
        "trailing_sl": 3480.0,
        "target": 3560.0,
        "entry_time_ist": "10:00:00 AM IST",
        "opened_at_utc": "2026-11-13T04:30:00Z",
        "status": "OPEN",
        "stage": "INITIAL (FIXED SL)",
    }

    tracker.active_positions[mcx_id] = {
        "position_id": mcx_id,
        "symbol": "GOLDPETAL",
        "sector": "COMMODITIES",
        "action": "BUY",
        "is_option": False,
        "entry_price": 7500.0,
        "current_price": 7520.0,
        "quantity": 1,
        "risk_r": 50.0,
        "initial_sl": 7450.0,
        "trailing_sl": 7450.0,
        "target": 7650.0,
        "entry_time_ist": "10:00:00 AM IST",
        "opened_at_utc": "2026-11-13T04:30:00Z",
        "status": "OPEN",
        "stage": "INITIAL (FIXED SL)",
    }

    # Simulate 15:02 IST
    time_1502 = datetime.datetime(2026, 11, 13, 15, 2, 0, tzinfo=IST)

    with patch("datetime.datetime") as mock_dt:
        mock_dt.now.side_effect = lambda tz=None: time_1502 if tz else datetime.datetime.now(datetime.timezone.utc)
        mock_dt.timezone = datetime.timezone
        mock_dt.timedelta = datetime.timedelta
        with patch.object(tracker, "_fetch_live_price", side_effect=lambda sym, sec: 3510.0 if sym == "TCS" else 7520.0):
            with patch.object(tracker.indmoney_feed, "fetch_batch_quotes"):
                tracker.update_and_trail_positions()

    # Equity position must be CLOSED due to 15:00 IST square-off
    assert eq_id not in tracker.active_positions, "Equity position should have closed at 15:00 IST"
    # MCX position must STAY OPEN
    assert mcx_id in tracker.active_positions, "MCX commodity position should NOT close at 15:00 IST"
    assert tracker.active_positions[mcx_id]["status"] == "OPEN"

