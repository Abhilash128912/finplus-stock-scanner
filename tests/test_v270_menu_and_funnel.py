"""
FinPlus+ Phase 6 Menu & Funnel Tests (v2.7.0)
Tests:
1. 15 triggers occur in a day: all 15 appear on the panel, NTFY sent for the best 12 only (capped at 12 triggers/day).
2. 30 simulated trades with 50% win rate produce Grade C and tag LOW CONFIDENCE on future trades of that setup.
3. Mark a trade 'taken': taken=1 persists in DB, survives restart, reflected in API response.
4. Trailing-stop NTFY fires for a TAKEN trade when the stop moves, does not fire for UNTAKEN trades, and does not fire more than once per 5 minutes per trade.
5. Weekly report endpoint returns correct suggestions_this_week, taken_this_week, taken_win_pct, and setup breakdown.
"""

import os
import sqlite3
import datetime
import time
from unittest.mock import patch, MagicMock
import pytest

from src.paper.live_paper_tracker import LivePaperTracker
from src.notifications.ntfy_dispatcher import NTFYDispatcher

@pytest.fixture
def mock_trading_hours():
    dt = datetime.datetime(2026, 11, 10, 10, 30, 0) # Tuesday 10:30 AM IST
    with patch("src.paper.live_paper_tracker.get_ist_now", return_value=dt), \
         patch.object(LivePaperTracker, "_get_now_ist", return_value=dt):
        yield dt

@pytest.fixture
def test_tracker(tmp_path, mock_trading_hours):
    db_file = str(tmp_path / "menu_test.db")
    tracker = LivePaperTracker(db_path=db_file)
    tracker.virtual_capital = 100_000.0
    return tracker

def test_15_triggers_capped_at_12_ntfy_alerts(test_tracker):
    """15 valid triggers occur in a day: up to 15 appear on panel, NTFY sent for first 12 only."""
    with patch.object(NTFYDispatcher, "send_trade_trigger_alert", return_value=True) as mock_ntfy, \
         patch.dict(os.environ, {"FORCE_LOCAL_NTFY": "1"}):
        
        opened_positions = []
        for i in range(15):
            sym = f"SYM{i}"
            pos = test_tracker.open_position(
                source_app="APP_02_STOCK_SCANNER",
                sector="INTRADAY_CASH",
                symbol=sym,
                action="BUY",
                entry_price=100.0 + i,
                stop_loss=99.5 + i, # Tight 0.5% SL
                target=0.0,
                quantity=10,
                notes=f"Trigger {i}"
            )
            if pos:
                opened_positions.append(pos)

        # Panel holds up to 15 positions
        assert len(opened_positions) == 15
        assert len(test_tracker.active_positions) == 15
        # NTFY trigger alert dispatched for at most 12 triggers
        assert mock_ntfy.call_count == 12

def test_30_trades_at_50_pct_win_produces_grade_c_and_low_confidence_tag(test_tracker):
    """30 simulated trades with 50% win rate produce Grade C and the tag LOW CONFIDENCE on future trades."""
    setup_key = "PDH_BREAKOUT_BUY_INTRADAY_CASH"

    # Seed 30 trades: 15 wins (+1.5R) and 15 losses (-1.0R) -> 50% win rate, avg_r = +0.25
    for i in range(15):
        test_tracker.record_setup_trade_result(setup_key, is_win=True, r_result=1.5, net_pnl=150.0)
    for i in range(15):
        test_tracker.record_setup_trade_result(setup_key, is_win=False, r_result=-1.0, net_pnl=-100.0)

    # Grade must be C (< 60% win rate)
    grade = test_tracker.get_setup_grade(setup_key)
    assert grade == "C"

    # Now open a new trade with this setup key
    with patch.object(test_tracker, "_dispatch_ntfy_trade_opened") as mock_ntfy:
        pos = test_tracker.open_position(
            source_app="APP_02_STOCK_SCANNER",
            sector="INTRADAY_CASH",
            symbol="HDFC",
            action="BUY",
            entry_price=1500.0,
            stop_loss=1490.0,
            target=0.0,
            quantity=10,
            setup_key=setup_key
        )
        assert pos is not None
        assert pos["grade"] == "C"
        assert "LOW CONFIDENCE" in pos["tags"]
        # Grade C suppresses NTFY alert
        mock_ntfy.assert_not_called()

def test_mark_trade_taken_persists_in_db_and_survives_restart(tmp_path, mock_trading_hours):
    """Marking a trade 'taken' sets taken=1 in DB and survives app restart."""
    db_file = str(tmp_path / "taken_test.db")
    tracker = LivePaperTracker(db_path=db_file)

    pos = tracker.open_position(
        source_app="APP_02_STOCK_SCANNER",
        sector="INTRADAY_CASH",
        symbol="AXISBANK",
        action="BUY",
        entry_price=1000.0,
        stop_loss=995.0,
        target=0.0,
        quantity=20
    )
    pos_id = pos["position_id"]

    # Toggle taken
    ok = tracker.set_trade_taken(pos_id, taken=True, real_entry=1001.50, real_qty=25)
    assert ok is True
    assert tracker.active_positions[pos_id]["taken"] == 1
    assert tracker.active_positions[pos_id]["real_entry"] == 1001.50
    assert tracker.active_positions[pos_id]["real_qty"] == 25

    # Simulate fresh app startup reading from same DB
    restarted_tracker = LivePaperTracker(db_path=db_file)
    assert pos_id in restarted_tracker.active_positions
    restarted_pos = restarted_tracker.active_positions[pos_id]
    assert restarted_pos["taken"] == 1
    assert restarted_pos["real_entry"] == 1001.50
    assert restarted_pos["real_qty"] == 25

def test_trailing_stop_ntfy_for_taken_trades_only_and_rate_limited(test_tracker):
    """Trailing-stop NTFY fires for TAKEN trade when stop advances, does not fire for UNTAKEN, and max 1/5m."""
    # 1. Untaken trade
    untaken_pos = test_tracker.open_position(
        source_app="APP_02_STOCK_SCANNER",
        sector="INTRADAY_CASH",
        symbol="TATAMOTORS",
        action="BUY",
        entry_price=100.0,
        stop_loss=99.2,
        target=0.0,
        quantity=30,
        atr_val=0.50
    )
    # 2. Taken trade
    taken_pos = test_tracker.open_position(
        source_app="APP_02_STOCK_SCANNER",
        sector="INTRADAY_CASH",
        symbol="BAJFINANCE",
        action="BUY",
        entry_price=100.0,
        stop_loss=99.2,
        target=0.0,
        quantity=30,
        atr_val=0.50
    )
    test_tracker.set_trade_taken(taken_pos["position_id"], taken=True)

    with patch.object(NTFYDispatcher, "send_trailing_stop_moved_alert") as mock_trail_ntfy:
        # Move both to +1R -> trailing stop advances
        def mock_price(sym, sec):
            return 101.50
        with patch.object(test_tracker, "_fetch_live_price", side_effect=mock_price):
            test_tracker.update_and_trail_positions()

        # Must fire for TAKEN trade only
        assert mock_trail_ntfy.call_count == 1
        call_arg = mock_trail_ntfy.call_args[0][0]
        assert call_arg["symbol"] == "BAJFINANCE"

        # Advance price again within 5 minutes (e.g. at 103.0) -> rate limiter blocks 2nd alert
        def mock_price_2(sym, sec):
            return 103.00
        with patch.object(test_tracker, "_fetch_live_price", side_effect=mock_price_2):
            test_tracker.update_and_trail_positions()

        # Still only 1 call because < 5 minutes elapsed
        assert mock_trail_ntfy.call_count == 1

def test_weekly_performance_report_metrics(test_tracker):
    """Weekly performance report accurately reflects taken win %, profit, and best setup."""
    today_ist = test_tracker._get_now_ist().strftime("%Y-%m-%d %I:%M:%S %p IST")
    opened_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()

    with test_tracker._get_connection() as conn:
        # Taken win: +₹500, +2.0R
        conn.execute("""
            INSERT INTO closed_paper_trades (
                trade_id, position_id, symbol, sector, action, is_option,
                entry_price, exit_price, quantity, gross_pnl, total_charges, net_pnl,
                exit_reason, entry_time_ist, exit_time_ist, opened_at_utc, closed_at_utc,
                taken, setup_key, grade, r_multiple
            ) VALUES ('TRD_1', 'P1', 'INFY', 'INTRADAY_CASH', 'BUY', 0, 100, 105, 100, 500, 20, 480, 'TRAILING_SL_HIT', ?, ?, ?, ?, 1, 'RANGE_BREAKOUT_BUY', 'A', 2.0)
        """, (today_ist, today_ist, opened_utc, opened_utc))

        # Taken loss: -₹200, -1.0R
        conn.execute("""
            INSERT INTO closed_paper_trades (
                trade_id, position_id, symbol, sector, action, is_option,
                entry_price, exit_price, quantity, gross_pnl, total_charges, net_pnl,
                exit_reason, entry_time_ist, exit_time_ist, opened_at_utc, closed_at_utc,
                taken, setup_key, grade, r_multiple
            ) VALUES ('TRD_2', 'P2', 'TCS', 'INTRADAY_CASH', 'BUY', 0, 100, 98, 100, -200, 20, -220, 'INITIAL_SL_HIT', ?, ?, ?, ?, 1, 'PDH_BREAKOUT_BUY', 'B', -1.0)
        """, (today_ist, today_ist, opened_utc, opened_utc))

        # Untaken win
        conn.execute("""
            INSERT INTO closed_paper_trades (
                trade_id, position_id, symbol, sector, action, is_option,
                entry_price, exit_price, quantity, gross_pnl, total_charges, net_pnl,
                exit_reason, entry_time_ist, exit_time_ist, opened_at_utc, closed_at_utc,
                taken, setup_key, grade, r_multiple
            ) VALUES ('TRD_3', 'P3', 'WIPRO', 'INTRADAY_CASH', 'BUY', 0, 100, 103, 100, 300, 20, 280, 'TRAILING_SL_HIT', ?, ?, ?, ?, 0, 'RANGE_BREAKOUT_BUY', 'A', 1.5)
        """, (today_ist, today_ist, opened_utc, opened_utc))
        conn.commit()

    report = test_tracker.get_weekly_performance_report()
    assert report["suggestions_this_week"] == 3
    assert report["taken_this_week"] == 2
    assert report["taken_win_pct"] == 50.0
    assert report["top_trusted_setup"] == "RANGE_BREAKOUT_BUY"

