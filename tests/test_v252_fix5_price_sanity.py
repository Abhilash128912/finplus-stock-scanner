"""
Acceptance Test for Item 5: Price Sanity Guard
- Reject any tick or option price that moves more than 15% (configurable) from last accepted price within one cycle, or is above 3x entry.
- Log WARNING, skip the tick, do not update P&L.
- If 3 consecutive ticks are rejected for one symbol, mark it DATA_SUSPECT and block signals for it.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_SCANNER_DIR = Path(__file__).resolve().parent.parent
if str(_SCANNER_DIR) not in sys.path:
    sys.path.insert(0, str(_SCANNER_DIR))

from src.paper.live_paper_tracker import LivePaperTracker


def test_price_sanity_rejects_spike_and_flags_suspect(tmp_path):
    db_file = tmp_path / "test_price_sanity.db"
    tracker = LivePaperTracker(db_path=str(db_file))
    tracker.price_sanity_pct = 15.0

    pos_id = "pos_sanity_test"
    tracker.active_positions[pos_id] = {
        "position_id": pos_id,
        "symbol": "FAKEPUMP",
        "sector": "INTRADAY_CASH",
        "action": "BUY",
        "is_option": False,
        "entry_price": 100.0,
        "current_price": 100.0,
        "quantity": 100,
        "risk_r": 2.0,
        "initial_sl": 98.0,
        "trailing_sl": 98.0,
        "target": 105.0,
        "entry_time_ist": "10:00:00 AM IST",
        "opened_at_utc": "2026-11-13T04:30:00Z",
        "status": "OPEN",
        "stage": "INITIAL (FIXED SL)",
        "unrealized_pnl": 0.0,
    }

    # 1. Inject a +40% spike tick (140.0) -> must be rejected, P&L unchanged
    with patch.object(tracker, "_fetch_live_price", return_value=140.0):
        with patch.object(tracker.indmoney_feed, "fetch_batch_quotes"):
            tracker.update_and_trail_positions()

    pos = tracker.active_positions[pos_id]
    assert pos["current_price"] == 100.0, "current_price was erroneously updated with +40% spike!"
    assert pos["unrealized_pnl"] == 0.0, "unrealized_pnl should remain unchanged on rejected tick!"
    assert tracker.rejected_ticks_count.get("FAKEPUMP") == 1

    # 2. Inject 2nd and 3rd consecutive bad ticks (145.0, 150.0)
    with patch.object(tracker, "_fetch_live_price", return_value=145.0):
        with patch.object(tracker.indmoney_feed, "fetch_batch_quotes"):
            tracker.update_and_trail_positions()
    assert tracker.rejected_ticks_count.get("FAKEPUMP") == 2
    assert "FAKEPUMP" not in tracker.suspect_symbols

    with patch.object(tracker, "_fetch_live_price", return_value=150.0):
        with patch.object(tracker.indmoney_feed, "fetch_batch_quotes"):
            tracker.update_and_trail_positions()
    assert tracker.rejected_ticks_count.get("FAKEPUMP") == 3
    # After 3 consecutive rejections, symbol must be in suspect_symbols (DATA_SUSPECT)
    assert "FAKEPUMP" in tracker.suspect_symbols, "Symbol was not flagged DATA_SUSPECT after 3 consecutive rejected ticks!"

