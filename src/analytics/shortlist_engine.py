"""
FinPlus+ Best-in-Class Shortlist & 30-Minute Trend Continuation Engine
Filters the single best setup per category every 30 minutes to eliminate overtrading:
1. Best Index Option (BSE Sensex prioritized / Nifty)
2. Best Stock Option (Lot <= 500 + Reliance)
3. Best Intraday Cash Stock
Tracks state across 30m intervals to emit "TREND CONTINUATION" when previous picks remain active.
"""

from typing import Dict, Any, List, Optional
from src.utils.time_and_logger import setup_logger, get_utc_iso, get_ist_time_str

logger = setup_logger("ShortlistEngine")

class ShortlistEngine:
    def __init__(self):
        self.previous_selection: Dict[str, Any] = {}
        self.current_selection: Dict[str, Any] = {}
        self.last_shortlist_utc: Optional[str] = None

    def select_best_candidates(self, scan_results: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Ranks all scanned candidates by points and selects the #1 best pick for:
        1. Index Option
        2. Stock Option
        3. Intraday Cash Stock
        """
        valid_candidates = [
            r for r in scan_results 
            if r.get("confluence_score", 0) >= 50 
            and not ("VETOED" in r.get("regime_confluence", ""))
            and r.get("signal_direction", 0) != 0
        ]
        
        # 1. Best Index Option (Sensex vs Nifty)
        index_candidates = [r for r in valid_candidates if r.get("category") == "INDEX_OPTIONS"]
        best_index = None
        if index_candidates:
            # Sensex priority weighting (+5 pts)
            def index_rank_key(item):
                score = item.get("confluence_score", 0)
                if item.get("symbol") == "SENSEX":
                    score += 5 # Sensex gets priority
                return score
            index_candidates.sort(key=index_rank_key, reverse=True)
            best_index = index_candidates[0]

        # 2. Best Stock Option (Lot <= 500 + Reliance)
        stock_candidates = [r for r in valid_candidates if r.get("category") == "STOCK_OPTIONS"]
        best_stock_option = None
        if stock_candidates:
            stock_candidates.sort(key=lambda x: x.get("confluence_score", 0), reverse=True)
            best_stock_option = stock_candidates[0]

        # 3. Best Intraday Cash Stock
        best_intraday_stock = None
        if stock_candidates:
            best_intraday_stock = stock_candidates[0] # Aligned with top momentum equity

        # 4. Check Trend Continuation vs Previous 30-Minute Cycle
        index_continuation = False
        stock_continuation = False
        
        prev_idx = self.previous_selection.get("best_index_option")
        if prev_idx and best_index:
            if prev_idx.get("symbol") == best_index.get("symbol") and prev_idx.get("signal_direction") == best_index.get("signal_direction"):
                index_continuation = True
                best_index["is_continuation"] = True
                
        prev_stk = self.previous_selection.get("best_stock_option")
        if prev_stk and best_stock_option:
            if prev_stk.get("symbol") == best_stock_option.get("symbol") and prev_stk.get("signal_direction") == best_stock_option.get("signal_direction"):
                stock_continuation = True
                best_stock_option["is_continuation"] = True

        selection_payload = {
            "timestamp_utc": get_utc_iso(),
            "ist_time": get_ist_time_str(),
            "best_index_option": best_index,
            "best_stock_option": best_stock_option,
            "best_intraday_stock": best_intraday_stock,
            "has_actionable_trades": (best_index is not None or best_stock_option is not None),
            "index_continuation": index_continuation,
            "stock_continuation": stock_continuation
        }

        # Save for next cycle comparison
        self.previous_selection = self.current_selection if self.current_selection else selection_payload
        self.current_selection = selection_payload
        self.last_shortlist_utc = selection_payload["timestamp_utc"]

        return selection_payload
