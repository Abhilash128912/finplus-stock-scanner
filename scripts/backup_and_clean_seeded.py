import os
import sys
import json
import shutil
import sqlite3
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKUP_DIR = Path(r"D:\AI_APPS\backups_paper_trades_20261115")
BACKUP_DIR.mkdir(parents=True, exist_ok=True)

SEEDED_IDS = {
    "TRENT_BUY_INTRADAY_CASH_20261113_110000",
    "RELIANCE_BUY_STOCK_OPTIONS_20261113_110000",
    "CRUDEOIL_BUY_COMMODITIES_20261113_110000",
    "TRD_TRENT_BUY_INTRADAY_CASH_20261113_110000",
    "TRD_RELIANCE_BUY_STOCK_OPTIONS_20261113_110000",
    "TRD_CRUDEOIL_BUY_COMMODITIES_20261113_110000",
}

def clean_local_sqlite():
    db_src = REPO_ROOT / "data" / "paper_trading.db"
    con = sqlite3.connect(db_src)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    
    # 1. Before counts
    cur.execute("SELECT position_id, symbol, notes FROM active_paper_positions")
    active_before = [dict(r) for r in cur.fetchall()]
    cur.execute("SELECT trade_id, symbol, exit_reason FROM closed_paper_trades")
    closed_before = [dict(r) for r in cur.fetchall()]
    
    seeded_active = [r for r in active_before if r["position_id"] in SEEDED_IDS or "20261113_110000" in r["position_id"]]
    seeded_closed = [r for r in closed_before if r["trade_id"] in SEEDED_IDS or "20261113_110000" in r["trade_id"]]
    
    print(f"=== BEFORE DELETION ===")
    print(f"Active positions total: {len(active_before)} (Seeded: {len(seeded_active)})")
    for s in seeded_active:
        print(f"  - Active Seeded: {s['position_id']} ({s['symbol']}) - {s.get('notes')}")
        
    print(f"Closed trades total: {len(closed_before)} (Seeded: {len(seeded_closed)})")
    for s in seeded_closed:
        print(f"  - Closed Seeded: {s['trade_id']} ({s['symbol']}) - {s.get('exit_reason')}")
        
    # 2. Delete strictly seeded trades
    cur.execute("""
        DELETE FROM active_paper_positions 
        WHERE position_id IN (
            'RELIANCE_BUY_STOCK_OPTIONS_20261113_110000',
            'TRENT_BUY_INTRADAY_CASH_20261113_110000',
            'CRUDEOIL_BUY_COMMODITIES_20261113_110000'
        ) OR position_id LIKE '%20261113_110000%'
    """)
    del_active_count = cur.rowcount

    cur.execute("""
        DELETE FROM closed_paper_trades 
        WHERE trade_id IN (
            'TRD_RELIANCE_BUY_STOCK_OPTIONS_20261113_110000',
            'TRD_TRENT_BUY_INTRADAY_CASH_20261113_110000',
            'TRD_CRUDEOIL_BUY_COMMODITIES_20261113_110000'
        ) OR position_id IN (
            'RELIANCE_BUY_STOCK_OPTIONS_20261113_110000',
            'TRENT_BUY_INTRADAY_CASH_20261113_110000',
            'CRUDEOIL_BUY_COMMODITIES_20261113_110000'
        ) OR trade_id LIKE '%20261113_110000%' OR position_id LIKE '%20261113_110000%'
    """)
    del_closed_count = cur.rowcount
    
    con.commit()
    
    # 3. After counts
    cur.execute("SELECT position_id, symbol FROM active_paper_positions")
    active_after = [dict(r) for r in cur.fetchall()]
    cur.execute("SELECT trade_id, symbol FROM closed_paper_trades")
    closed_after = [dict(r) for r in cur.fetchall()]
    
    remaining_seeded_active = [r for r in active_after if r["position_id"] in SEEDED_IDS or "20261113_110000" in r["position_id"]]
    remaining_seeded_closed = [r for r in closed_after if r["trade_id"] in SEEDED_IDS or "20261113_110000" in r["trade_id"]]
    
    print(f"\n=== AFTER DELETION ===")
    print(f"Deleted from active: {del_active_count}")
    print(f"Deleted from closed: {del_closed_count}")
    print(f"Remaining active positions: {len(active_after)} (Remaining Seeded: {len(remaining_seeded_active)})")
    print(f"Remaining closed trades: {len(closed_after)} (Remaining Seeded: {len(remaining_seeded_closed)})")
    print(f"Remaining active symbols: {[r['symbol'] for r in active_after]}")
    
    con.close()

if __name__ == "__main__":
    clean_local_sqlite()
