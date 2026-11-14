"""
scripts/list_fno_universe.py
Outputs qualifying F&O stocks from the loaded lot file:
- symbol, lot size, lot file date, total count
- Rule: lot size strictly < 500, plus RELIANCE, plus 3 index options (NIFTY, SENSEX, MIDCPNIFTY)
"""

import os
import sys
from pathlib import Path

# Add project root to sys.path
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.feeds.fno_contract_master import FNOContractMaster

def main():
    master = FNOContractMaster()
    universe = master.load_universe()
    
    indices = [x for x in universe if x.get("category") == "INDEX_OPTIONS"]
    equities = [x for x in universe if x.get("category") == "STOCK_OPTIONS"]

    print("================================================================================")
    print("FINPLUS+ QUALIFYING F&O UNIVERSE (Rule: Lot < 500 + RELIANCE + Index Options)")
    print("================================================================================")
    print(f"Contract Master Cache Date / Timestamp: {master.last_load_utc}")
    print(f"Total Instruments in Universe: {len(universe)}")
    print(f"Index Options Count: {len(indices)}")
    print(f"Qualifying F&O Equities Count: {len(equities)}")
    print("--------------------------------------------------------------------------------")
    print(f"{'TYPE':<15} | {'SYMBOL':<15} | {'LOT SIZE':<10} | {'NAME / UNDERLYING':<30}")
    print("--------------------------------------------------------------------------------")
    
    for idx in indices:
        print(f"{idx.get('category', 'INDEX'):<15} | {idx['symbol']:<15} | {idx.get('lot_size', '--'):<10} | {idx.get('name', '--'):<30}")

    print("--------------------------------------------------------------------------------")
    for eq in equities:
        priority_mark = " (PRIORITY)" if eq.get("is_priority_leader") else ""
        sym_str = f"{eq['symbol']}{priority_mark}"
        print(f"{eq.get('category', 'EQUITY'):<15} | {sym_str:<15} | {eq.get('lot_size', '--'):<10} | {eq.get('name', '--'):<30}")

    print("================================================================================")
    print(f"VERIFICATION SUMMARY:")
    print(f"- Index Options: {len(indices)} ({', '.join([x['symbol'] for x in indices])})")
    print(f"- F&O Equities strictly < 500 lot: {len([e for e in equities if e['symbol'] != 'RELIANCE'])}")
    print(f"- Exceptions (always_include): RELIANCE (lot size = {[e['lot_size'] for e in equities if e['symbol'] == 'RELIANCE'][0]})")
    print(f"- Total Tracked Instruments: {len(universe)}")
    print("================================================================================")

if __name__ == "__main__":
    main()
