import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.feeds.fno_contract_master import FNOContractMaster

m = FNOContractMaster(max_lot_size=500)
u = m.load_universe()
eq = sorted([x for x in u if x.get('category') == 'STOCK_OPTIONS'], key=lambda x: (x.get('lot_size', 0), x['symbol']))
indices = [x for x in u if x.get('category') == 'INDEX_OPTIONS']

print(f"Total instruments in universe: {len(u)}")
print(f"Indices ({len(indices)}):")
for idx in indices:
    print(f"  {idx['symbol']}: lot {idx.get('lot_size')}")

print(f"\nEquities qualifying: {len(eq)}")
for x in eq:
    sym = x['symbol']
    lot = x.get('lot_size')
    is_rel = "(RELIANCE)" if sym == "RELIANCE" else ""
    is_500 = "(EXACTLY 500)" if lot == 500 and sym != "RELIANCE" else ""
    print(f"  {sym:<15} : {lot:>4} {is_rel} {is_500}")

lot_under_500 = [x for x in eq if x.get('lot_size', 0) < 500]
lot_exact_500 = [x for x in eq if x.get('lot_size', 0) == 500]
print(f"\nSummary:")
print(f"  Lot < 500: {len(lot_under_500)} stocks")
print(f"  Lot == 500: {len(lot_exact_500)} stocks (including Reliance)")
