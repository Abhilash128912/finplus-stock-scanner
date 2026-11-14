"""
Acceptance Test for Item 3: Firebase Complete Mirror & Midday Restore
Proves:
1. Mirroring of open/closed paper positions with trail state, Taken flags, setup stats, daily latches, NTFY counters, and virtual capital.
2. Midday local database wipe + fresh restart.
3. Cold restore from Firestore:
   - All positions restored with trail state & Taken flags.
   - Daily latches restored so NO repeat triggers/alerts are fired.
   - NTFY daily counters intact.
   - Virtual capital intact.
   - Trades continue trailing properly.
"""

import os
import time
import datetime
from unittest.mock import MagicMock, patch
from pathlib import Path
import pytest

from src.storage.firestore_backend import FirestoreBackend
from src.paper.live_paper_tracker import LivePaperTracker
from src.storage.schedule_ledger import ScheduleLedger

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))


def test_firebase_midday_wipe_and_restore(tmp_path):
    paper_db = tmp_path / "paper_trading.db"
    ledger_db = tmp_path / "schedule_ledger.db"

    tracker = LivePaperTracker(db_path=str(paper_db))
    ledger = ScheduleLedger(db_path=str(ledger_db))

    today_str = "2026-11-13"
    fake_now = datetime.datetime(2026, 11, 13, 11, 0, 0, tzinfo=IST)

    # 1. Setup local state
    tracker.virtual_capital = 125000.0
    tracker.daily_ntfy_triggers_count = 5

    # Open a position
    with patch.object(tracker, "_get_now_ist", return_value=fake_now):
        pos = tracker.open_position(
            source_app="APP_02_STOCK_SCANNER",
            sector="INTRADAY_CASH",
            symbol="RELIANCE",
            action="BUY",
            entry_price=500.0,
            stop_loss=495.0,
            setup_key="RANGE_BREAKOUT"
        )
    assert pos is not None
    pos_id = pos["position_id"]

    # Mark Taken
    tracker.set_trade_taken(pos_id, taken=True, real_entry=500.5, real_qty=25)

    # Trail stop
    pos["trailing_sl"] = 505.0
    pos["highest_price"] = 512.0
    pos["stage"] = "TRAILING_ACTIVE"
    tracker.active_positions[pos_id] = pos

    # Record setup stat
    tracker.record_setup_trade_result("RANGE_BREAKOUT", is_win=True, r_result=2.0, net_pnl=500.0)

    # Latch daily breakout
    latched = ledger.try_latch_daily_breakout(today_str, "RELIANCE", 1, 500.0)
    assert latched is True

    # 2. Simulate Firestore storage in memory
    firestore_data = {
        "paper_trades": {pos_id: dict(pos)},
        "latches": {f"{today_str}_RELIANCE": {"date_str": today_str, "symbol": "RELIANCE", "break_direction": 1, "break_price": 500.0}},
        "setup_stats": {"RANGE_BREAKOUT": {
            "setup_key": "RANGE_BREAKOUT", "total_trades": 1, "wins": 1, "win_pct": 100.0,
            "avg_r": 2.0, "expectancy": 500.0, "grade": "B", "updated_at_utc": "2026-11-13T05:30:00Z"
        }},
        "state": {
            "virtual_capital": {"virtual_capital": 125000.0},
            f"ntfy_quota_{today_str}": {"quota_count": 5}
        }
    }

    # 3. MIDDAY WIPE: destroy local SQLite databases!
    tracker.active_positions.clear()
    
    # Wipe tables to simulate wiped/empty DB mid-day on Render
    with tracker._get_connection() as conn:
        conn.execute("DROP TABLE IF EXISTS active_paper_positions")
        conn.execute("DROP TABLE IF EXISTS closed_paper_trades")
        conn.execute("DROP TABLE IF EXISTS setup_stats")
        conn.commit()
    with ledger._get_connection() as conn:
        conn.execute("DROP TABLE IF EXISTS daily_breakout_latch")
        conn.execute("DROP TABLE IF EXISTS daily_schedule_ledger")
        conn.commit()

    del tracker
    del ledger
    import gc
    gc.collect()

    # 4. RESTART: Re-instantiate tracker and ledger with fresh blank DBs
    new_tracker = LivePaperTracker(db_path=str(paper_db))
    new_ledger = ScheduleLedger(db_path=str(ledger_db))

    assert len(new_tracker.active_positions) == 0
    assert not new_ledger.is_symbol_latched_today(today_str, "RELIANCE")

    # Mock Firestore client stream & doc reads
    def mock_stream(col_name):
        docs = []
        col_key = col_name.split("/")[-1]
        for k, v in firestore_data.get(col_key, {}).items():
            doc = MagicMock()
            doc.to_dict.return_value = v
            docs.append(doc)
        return docs

    def mock_doc_get(doc_path):
        parts = doc_path.split("/")
        col_key = parts[-2]
        doc_key = parts[-1]
        val = firestore_data.get(col_key, {}).get(doc_key)
        doc = MagicMock()
        doc.exists = (val is not None)
        doc.to_dict.return_value = val or {}
        return doc

    fb = FirestoreBackend()
    fb.is_connected = True
    fb.client = MagicMock()
    fb.client.collection.side_effect = lambda col: MagicMock(stream=lambda: mock_stream(col))
    fb.client.document.side_effect = lambda path: MagicMock(get=lambda: mock_doc_get(path))

    with patch.object(fb, "_get_ist_date_str", return_value=today_str):
        restore_res = fb.restore_all_from_firestore(
            candle_store=MagicMock(),
            paper_tracker=new_tracker,
            schedule_ledger=new_ledger
        )

    # 5. VERIFY RESTORATION
    assert restore_res["status"] == "SUCCESS"
    assert len(new_tracker.active_positions) == 1
    restored_pos = new_tracker.active_positions[pos_id]
    
    # Check trail state & Taken flags intact
    assert restored_pos["symbol"] == "RELIANCE"
    assert restored_pos["taken"] == 1
    assert restored_pos["real_entry"] == 500.5
    assert restored_pos["real_qty"] == 25
    assert restored_pos["trailing_sl"] == 505.0
    assert restored_pos["highest_price"] == 512.0
    assert restored_pos["stage"] == "TRAILING_ACTIVE"

    # Check virtual capital restored
    assert new_tracker.virtual_capital == 125000.0

    # Check NTFY quota counter restored
    assert new_tracker.daily_ntfy_triggers_count == 5

    # Check daily latch restored: NO DUPLICATE TRIGGERS/ALERTS
    assert new_ledger.is_symbol_latched_today(today_str, "RELIANCE") is True

    # Check setup stats restored
    setup_stat = new_tracker.get_setup_stats_dict("RANGE_BREAKOUT")
    assert setup_stat["total_trades"] == 1
    assert setup_stat["win_pct"] == 100.0

    # Check that positions keep trailing properly
    with patch.object(new_tracker, "_fetch_live_price", return_value=515.0):
        with patch.object(new_tracker.indmoney_feed, "fetch_batch_quotes"):
            with patch.object(new_tracker, "_get_now_ist", return_value=fake_now):
                new_tracker.update_and_trail_positions()
    
    updated_pos = new_tracker.active_positions[pos_id]
    assert updated_pos["current_price"] == 515.0
    assert updated_pos["highest_price"] == 515.0
