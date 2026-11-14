import asyncio
import os
import sys
import time
import datetime
from pathlib import Path
from unittest.mock import patch

# Add project root
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from src.paper.live_paper_tracker import LivePaperTracker

def seed_demo_trades():
    db_path = str(_ROOT / "data" / "paper_trading.db")
    tracker = LivePaperTracker(db_path=db_path)
    
    # Mock weekday 11:00 AM IST to allow positions to open
    fake_now = datetime.datetime(2026, 11, 13, 11, 0, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=5, minutes=30)))
    
    with patch.object(tracker, "_get_now_ist", return_value=fake_now):
        # 1. Setup stats seeding
        tracker.record_setup_trade_result("RANGE_BREAKOUT_BUY_INTRADAY_CASH", is_win=True, r_result=2.1, net_pnl=4200.0)
        tracker.record_setup_trade_result("PDH_BREAKOUT_BUY_STOCK_OPTIONS", is_win=True, r_result=1.8, net_pnl=3600.0)
        tracker.record_setup_trade_result("VWAP_RECLAIM_BUY_INTRADAY_CASH", is_win=False, r_result=-1.0, net_pnl=-2000.0)
        
        # 2. Open 4 diverse paper trades
        # Trade 1: Grade A Range Breakout (Trailing)
        p1 = tracker.open_position(
            source_app="APP_02_STOCK_SCANNER",
            sector="INTRADAY_CASH",
            symbol="TRENT",
            action="BUY",
            entry_price=6400.0,
            stop_loss=6340.0,
            quantity=3,
            notes="Range Breakout above ₹6380",
            setup_key="RANGE_BREAKOUT_BUY_INTRADAY_CASH"
        )
        if p1:
            pid = p1["position_id"]
            tracker.active_positions[pid]["current_price"] = 6520.0
            tracker.active_positions[pid]["highest_price"] = 6540.0
            tracker.active_positions[pid]["trailing_sl"] = 6460.0
            tracker.active_positions[pid]["stage"] = "TRAILING_RATIFIED"
            tracker.active_positions[pid]["unrealized_pnl"] = (6520.0 - 6400.0) * 3
            tracker.active_positions[pid]["pnl_pct"] = round((6520.0 - 6400.0) / 6400.0 * 100, 2)
            tracker.set_trade_taken(pid, taken=True, real_entry=6402.5, real_qty=3)
        
        # Trade 2: Stock Option (F&O liquid under 500)
        p2 = tracker.open_position(
            source_app="APP_02_STOCK_SCANNER",
            sector="STOCK_OPTIONS",
            symbol="RELIANCE 2500 CE",
            action="BUY",
            entry_price=45.0,
            stop_loss=31.5,
            quantity=250,
            notes="PDH Breakout above ₹2495",
            setup_key="PDH_BREAKOUT_BUY_STOCK_OPTIONS"
        )
        if p2:
            pid = p2["position_id"]
            tracker.active_positions[pid]["current_price"] = 52.0
            tracker.active_positions[pid]["highest_price"] = 54.0
            tracker.active_positions[pid]["trailing_sl"] = 45.0 # Breakeven trailed
            tracker.active_positions[pid]["stage"] = "TRAILING_BREAKEVEN"
            tracker.active_positions[pid]["unrealized_pnl"] = (52.0 - 45.0) * 250
            tracker.active_positions[pid]["pnl_pct"] = round((52.0 - 45.0) / 45.0 * 100, 2)

        # Trade 3: Commodity (Crude Oil)
        p3 = tracker.open_position(
            source_app="APP_02_STOCK_SCANNER",
            sector="COMMODITIES",
            symbol="CRUDEOIL",
            action="BUY",
            entry_price=6120.0,
            stop_loss=6060.0,
            quantity=100,
            notes="MCX Evening Session Breakout",
            setup_key="RANGE_BREAKOUT_BUY_COMMODITIES"
        )
        if p3:
            pid = p3["position_id"]
            tracker.active_positions[pid]["current_price"] = 6185.0
            tracker.active_positions[pid]["highest_price"] = 6195.0
            tracker.active_positions[pid]["trailing_sl"] = 6120.0
            tracker.active_positions[pid]["stage"] = "TRAILING_BREAKEVEN"
            tracker.active_positions[pid]["unrealized_pnl"] = (6185.0 - 6120.0) * 100
            tracker.active_positions[pid]["pnl_pct"] = round((6185.0 - 6120.0) / 6120.0 * 100, 2)
            
            # Close trade 3 to demonstrate closed trade tab
            tracker.close_position(pid, exit_price=6180.0, exit_reason="TRAILING_STOP_HIT")
        
        print("Demo trades successfully seeded into local database!")

if __name__ == "__main__":
    seed_demo_trades()
