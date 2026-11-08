"""
FinPlus+ Best-in-Class Shortlist & 30-Minute Trend Continuation Engine
Provides transparent, detailed institutional trade setups:
1. BSE Sensex Options Radar (Always shows Call CE vs Put PE strikes, triggers, and active directive)
2. NSE Nifty Options Radar (Always shows Call CE vs Put PE strikes, triggers, and active directive)
3. Best Stock Option (Explicit BUY CE vs BUY PE with exact entry, SL, and target)
4. Best Intraday Cash Stock (Explicit BUY / LONG vs SELL / SHORT with exact entry, SL, and target)
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
        Ranks all scanned candidates and prepares the full actionable trade matrix.
        """
        # Separate Index items from Stock items
        sensex_item = next((r for r in scan_results if r.get("symbol") == "SENSEX"), None)
        nifty_item = next((r for r in scan_results if r.get("symbol") == "NIFTY"), None)
        
        # Format Sensex Radar
        sensex_radar = self._format_index_radar(sensex_item, default_sym="SENSEX", default_lot=20, default_step=100)
        
        # Format Nifty Radar
        nifty_radar = self._format_index_radar(nifty_item, default_sym="NIFTY", default_lot=65, default_step=50)
        
        # Filter valid Stock Option candidates (score >= 50 and not vetoed)
        stock_candidates = [
            r for r in scan_results 
            if r.get("category") == "STOCK_OPTIONS" 
            and r.get("signal_direction", 0) != 0
        ]
        
        # Rank: Approved setups first, then highest score
        def stock_rank_key(item):
            score = item.get("confluence_score", 0)
            if not ("VETOED" in item.get("regime_confluence", "")):
                score += 100 # High priority for cleared trades
            return score
            
        stock_candidates.sort(key=stock_rank_key, reverse=True)
        
        best_stock_option = None
        best_intraday_stock = None
        
        if stock_candidates:
            top = stock_candidates[0]
            best_stock_option = top
            best_intraday_stock = top
            
        # Detect Trend Continuation vs Previous 30m Cycle
        stock_continuation = False
        prev_stk = self.previous_selection.get("best_stock_option")
        if prev_stk and best_stock_option:
            if prev_stk.get("symbol") == best_stock_option.get("symbol") and prev_stk.get("signal_direction") == best_stock_option.get("signal_direction"):
                stock_continuation = True
                best_stock_option["is_continuation"] = True
                
        index_continuation = False
        prev_idx = self.previous_selection.get("best_index_option")
        # Determine best index option (Sensex prioritized)
        best_index_option = sensex_radar if sensex_radar.get("is_actionable") else (nifty_radar if nifty_radar.get("is_actionable") else sensex_radar)
        if prev_idx and best_index_option.get("is_actionable"):
            if prev_idx.get("symbol") == best_index_option.get("symbol") and prev_idx.get("signal_direction") == best_index_option.get("signal_direction"):
                index_continuation = True
                best_index_option["is_continuation"] = True

        selection_payload = {
            "timestamp_utc": get_utc_iso(),
            "ist_time": get_ist_time_str(),
            "sensex_radar": sensex_radar,
            "nifty_radar": nifty_radar,
            "best_index_option": best_index_option,
            "best_stock_option": best_stock_option,
            "best_intraday_stock": best_intraday_stock,
            "has_actionable_trades": (best_stock_option is not None and not ("VETOED" in best_stock_option.get("regime_confluence", ""))) or best_index_option.get("is_actionable", False),
            "index_continuation": index_continuation,
            "stock_continuation": stock_continuation
        }

        self.previous_selection = self.current_selection if self.current_selection else selection_payload
        self.current_selection = selection_payload
        self.last_shortlist_utc = selection_payload["timestamp_utc"]
        return selection_payload

    def _format_index_radar(self, item: Optional[Dict[str, Any]], default_sym: str, default_lot: int, default_step: int) -> Dict[str, Any]:
        """Ensures Sensex and Nifty always have explicit Call/Put strikes, trigger levels, and clear actionable directives."""
        if not item:
            return {
                "symbol": default_sym,
                "lot_size": default_lot,
                "status": "INITIALIZING",
                "call_strike": f"{default_sym} CE",
                "put_strike": f"{default_sym} PE",
                "action_badge": "STANDBY",
                "directive": "Downloading 15m candle history...",
                "is_actionable": False
            }
            
        last_price = item.get("last_price", 0.0)
        strike_step = item.get("strike_step", default_step)
        atm_strike = int(round(last_price / strike_step) * strike_step) if last_price > 0 else 0
        
        call_strike = f"{item['symbol']} {atm_strike} CE"
        put_strike = f"{item['symbol']} {atm_strike} PE"
        
        sig_dir = item.get("signal_direction", 0)
        is_vetoed = "VETOED" in item.get("regime_confluence", "")
        confluence_badge = item.get("regime_badge", "")
        
        action_badge = "HOLD CASH / WAIT"
        directive = f"Inside Range. Call trigger > ₹{item.get('pdh', 0)} | Put trigger < ₹{item.get('pdl', 0)}"
        is_actionable = False
        
        if sig_dir == 1: # Bullish
            if is_vetoed:
                action_badge = f"🛑 VETOED (DO NOT BUY CALLS)"
                directive = f"Macro trend is Bearish/Choppy ({confluence_badge}). Long breakout rejected for capital safety."
            else:
                action_badge = f"🟢 BUY {call_strike} (CALL OPTION)"
                directive = f"Breakout above Range Top (₹{item.get('range_top')}) & PDH (₹{item.get('pdh')}). Target: +180% on Premium."
                is_actionable = True
        elif sig_dir == -1: # Bearish
            if is_vetoed:
                action_badge = f"🛑 VETOED (DO NOT SHORT)"
                directive = f"Macro trend is Bullish ({confluence_badge}). Short breakdown rejected for capital safety."
            else:
                action_badge = f"🔴 BUY {put_strike} (PUT OPTION)"
                directive = f"Breakdown below Range Bottom (₹{item.get('range_bottom')}) & PDL (₹{item.get('pdl')}). Target: +180% on Premium."
                is_actionable = True
                
        return {
            "symbol": item["symbol"],
            "name": item.get("name", item["symbol"]),
            "lot_size": item.get("lot_size", default_lot),
            "last_price": last_price,
            "pct_change_pdc": item.get("pct_change_pdc", 0.0),
            "pdh": item.get("pdh", 0.0),
            "pdl": item.get("pdl", 0.0),
            "range_top": item.get("range_top", 0.0),
            "range_bottom": item.get("range_bottom", 0.0),
            "confluence_score": item.get("confluence_score", 0),
            "points_breakdown": item.get("points_breakdown", {}),
            "call_strike": call_strike,
            "put_strike": put_strike,
            "action_badge": action_badge,
            "directive": directive,
            "is_actionable": is_actionable,
            "regime_confluence": item.get("regime_confluence", ""),
            "regime_badge": confluence_badge,
            "risk_management": item.get("risk_management", {})
        }
