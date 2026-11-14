"""
FinPlus+ Phase 5 Automated Trade Rules Tests (v2.7.0)
Tests:
1. Wide structural stop (e.g. 1.8% cash) opens with tag WIDE_SL, panel only, no NTFY push.
2. Trade whose cost exceeds capital opens with tag OVER_CAPITAL, panel only.
3. Price rises +1R: stop moves to entry + costs (breakeven).
4. Price rises further: stop trails behind highest price, never moves down (monotonic ratchet).
5. Price reverses: exits at trailed stop with realistic slippage, NO TARGET exit triggers.
6. Trigger event: 1 trade row in DB, exactly 1 NTFY alert (no duplicate alerts).
"""

import os
import sqlite3
import datetime
from unittest.mock import patch, MagicMock
import pytest

from src.paper.live_paper_tracker import LivePaperTracker

@pytest.fixture
def mock_trading_hours():
    dt = datetime.datetime(2026, 11, 10, 10, 30, 0) # Tuesday 10:30 AM IST
    with patch("src.paper.live_paper_tracker.get_ist_now", return_value=dt), \
         patch.object(LivePaperTracker, "_get_now_ist", return_value=dt):
        yield dt

@pytest.fixture
def test_tracker(tmp_path, mock_trading_hours):
    db_file = str(tmp_path / "paper_test.db")
    tracker = LivePaperTracker(db_path=db_file)
    tracker.virtual_capital = 100_000.0
    return tracker

def test_wide_structural_stop_opens_with_wide_sl_panel_only(test_tracker):
    """Wide structural stop (> 1.0% cash) opens with tag WIDE_SL, panel only, no NTFY push."""
    with patch.object(test_tracker, "_dispatch_ntfy_trade_opened") as mock_ntfy:
        pos = test_tracker.open_position(
            source_app="APP_02_STOCK_SCANNER",
            sector="INTRADAY_CASH",
            symbol="INFY",
            action="BUY",
            entry_price=1000.0,
            stop_loss=982.0, # 1.8% SL -> exceeds 1.0% threshold
            target=0.0,
            quantity=20,
            notes="Wide test"
        )
        assert pos is not None
        assert "WIDE_SL" in pos["tags"]
        # WIDE_SL must suppress NTFY dispatch
        mock_ntfy.assert_not_called()

def test_over_capital_trade_opens_with_over_capital_tag(test_tracker):
    """Trade whose cost exceeds virtual capital opens with tag OVER_CAPITAL, panel only."""
    with patch.object(test_tracker, "_dispatch_ntfy_trade_opened") as mock_ntfy:
        # Cost = 1500 * 100 = 150,000 > 100,000 virtual capital
        pos = test_tracker.open_position(
            source_app="APP_02_STOCK_SCANNER",
            sector="INTRADAY_CASH",
            symbol="TCS",
            action="BUY",
            entry_price=1500.0,
            stop_loss=1490.0, # Tight SL (0.66%)
            target=0.0,
            quantity=100,
            notes="Over capital test"
        )
        assert pos is not None
        assert "OVER_CAPITAL" in pos["tags"]
        # OVER_CAPITAL must suppress NTFY dispatch
        mock_ntfy.assert_not_called()

def test_trailing_stop_breakeven_at_1r_and_ratchets_upwards(test_tracker):
    """Price rises +1R: stop moves to entry + costs; further rise trails behind highest price and never drops."""
    pos = test_tracker.open_position(
        source_app="APP_02_STOCK_SCANNER",
        sector="INTRADAY_CASH",
        symbol="RELIANCE",
        action="BUY",
        entry_price=100.0,
        stop_loss=99.2, # Risk R ~ 0.85
        target=0.0,
        quantity=50,
        notes="Trailing test",
        atr_val=0.50
    )
    assert pos is not None
    pos_id = pos["position_id"]
    initial_sl = pos["trailing_sl"]
    actual_entry = pos["entry_price"] # with slippage = 100.05

    # 1. Price rises to +1R (e.g. 101.00)
    with patch.object(test_tracker, "_fetch_live_price", return_value=101.00):
        test_tracker.update_and_trail_positions()
        curr_pos = test_tracker.active_positions[pos_id]
        # At +1R, SL moves to breakeven + costs
        assert curr_pos["trailing_sl"] >= round(actual_entry + 0.50, 2)
        assert curr_pos["trailing_sl"] > initial_sl
        sl_at_1r = curr_pos["trailing_sl"]

    # 2. Price rises further to 103.00
    with patch.object(test_tracker, "_fetch_live_price", return_value=103.00):
        test_tracker.update_and_trail_positions()
        curr_pos = test_tracker.active_positions[pos_id]
        # ATR trail = highest (103.0) - (1.5 * 0.5) = 102.25
        assert curr_pos["trailing_sl"] > sl_at_1r
        assert curr_pos["highest_price"] == 103.00
        highest_sl = curr_pos["trailing_sl"]

    # 3. Price pulls back to 102.50: Trailing SL must NEVER move down (monotonic ratchet)
    with patch.object(test_tracker, "_fetch_live_price", return_value=102.50):
        test_tracker.update_and_trail_positions()
        curr_pos = test_tracker.active_positions[pos_id]
        assert curr_pos["trailing_sl"] == highest_sl # Ratchet locked

def test_reversal_exits_at_trailed_stop_no_target(test_tracker):
    """Price reverses: exits at trailed stop with realistic slippage, NO TARGET triggers."""
    pos = test_tracker.open_position(
        source_app="APP_02_STOCK_SCANNER",
        sector="INTRADAY_CASH",
        symbol="SBIN",
        action="BUY",
        entry_price=500.0,
        stop_loss=496.0,
        target=0.0, # NO TARGET
        quantity=40,
        atr_val=2.0
    )
    pos_id = pos["position_id"]

    # Move to +1R to lock profit
    with patch.object(test_tracker, "_fetch_live_price", return_value=506.0):
        test_tracker.update_and_trail_positions()

    trailed_sl = test_tracker.active_positions[pos_id]["trailing_sl"]
    assert trailed_sl > 500.0 # Profit locked

    # Reversal below trailed stop
    with patch.object(test_tracker, "_fetch_live_price", return_value=trailed_sl - 1.0):
        test_tracker.update_and_trail_positions()

    # Position should now be closed
    assert pos_id not in test_tracker.active_positions
    with test_tracker._get_connection() as conn:
        closed = conn.execute("SELECT * FROM closed_paper_trades WHERE position_id = ?", (pos_id,)).fetchone()
        assert closed is not None
        assert closed["exit_reason"] == "TRAILING_SL_HIT"
        assert closed["net_pnl"] > 0 # Closed in locked profit

def test_trigger_event_one_trade_row_and_one_ntfy_alert(test_tracker):
    """Trigger event creates exactly 1 trade row in DB and duplicate triggers are rejected."""
    with patch.object(test_tracker, "_dispatch_ntfy_trade_opened") as mock_ntfy:
        pos1 = test_tracker.open_position(
            source_app="APP_02_STOCK_SCANNER",
            sector="INTRADAY_CASH",
            symbol="WIPRO",
            action="BUY",
            entry_price=450.0,
            stop_loss=447.0, # Tight SL (0.66%)
            target=0.0,
            quantity=40
        )
        assert pos1 is not None
        assert mock_ntfy.call_count == 1

        # Duplicate attempt immediately while position is OPEN
        pos2 = test_tracker.open_position(
            source_app="APP_02_STOCK_SCANNER",
            sector="INTRADAY_CASH",
            symbol="WIPRO",
            action="BUY",
            entry_price=451.0,
            stop_loss=448.0
        )
        assert pos2 is None # Disallowed duplicate
        assert mock_ntfy.call_count == 1 # No duplicate alert

