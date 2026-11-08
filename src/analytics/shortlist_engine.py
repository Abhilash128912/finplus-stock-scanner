"""
FinPlus+ Best-in-Class Shortlist & Trend Continuation Engine
STRICT INSTITUTIONAL MARKET REGIME GATING:
1. Never suggest CE and PE simultaneously.
2. Market conditions (App #1 Air Traffic Control) MUST confirm the direction before any strike is recommended.
3. If market regime is Choppy, Neutral, or Vetoed, output HOLD CASH (Zero options recommended).
4. Stock Options and Intraday Cash are completely decoupled.
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
        Ranks all scanned candidates with strict market-condition gates.
        VETOED trades are strictly disqualified from being recommended.
        """
        # Extract regime context for clear market diagnostics
        regime = scan_results[0].get("regime_data", {}) if scan_results else {}
        trend = regime.get("trend", {})
        trend_score = trend.get("score", 0)
        trend_label = trend.get("label", "CHOPPY / CONSOLIDATING")
        heavyweights = regime.get("metrics", {}).get("heavyweights_above", "0/5")
        
        # Separate Index items from Stock items
        sensex_item = next((r for r in scan_results if r.get("symbol") == "SENSEX"), None)
        nifty_item = next((r for r in scan_results if r.get("symbol") == "NIFTY"), None)
        
        # Format Sensex Radar (Never suggests CE and PE at the same time)
        sensex_radar = self._format_index_radar(sensex_item, default_sym="SENSEX", default_lot=20, default_step=100, regime=regime)
        
        # Format Nifty Radar (Never suggests CE and PE at the same time)
        nifty_radar = self._format_index_radar(nifty_item, default_sym="NIFTY", default_lot=65, default_step=50, regime=regime)
        
        # -------------------------------------------------------------
        # 1. STOCK OPTIONS: STRICT MARKET CONDITION FILTER
        # Must be: F&O (lot <= 500) + Score >= 50 + APPROVED by App #1 Macro Regime
        # -------------------------------------------------------------
        approved_stock_options = [
            r for r in scan_results 
            if r.get("category") == "STOCK_OPTIONS" 
            and r.get("signal_direction", 0) != 0
            and r.get("confluence_score", 0) >= 50
            and "APPROVED" in r.get("regime_confluence", "")  # MUST PASS MARKET CONDITIONS TEST
        ]
        
        # Sort by highest confluence score
        approved_stock_options.sort(key=lambda x: x.get("confluence_score", 0), reverse=True)
        best_stock_option = approved_stock_options[0] if approved_stock_options else None
        
        # Find top candidate on watch if gated by market conditions
        coiled_stock_options = [
            r for r in scan_results 
            if r.get("category") == "STOCK_OPTIONS" 
            and r.get("signal_direction", 0) != 0
            and r.get("confluence_score", 0) >= 50
        ]
        coiled_stock_options.sort(key=lambda x: x.get("confluence_score", 0), reverse=True)
        top_watch_option = coiled_stock_options[0] if coiled_stock_options else None
        
        if top_watch_option:
            stock_option_standby_reason = f"Market Choppy: Top setup {top_watch_option['symbol']} scored {top_watch_option.get('confluence_score')}/100, but Index momentum (Score: +{trend_score}/100, Heavyweights: {heavyweights} above VWAP) is below entry threshold. Option buying paused to prevent theta decay."
        else:
            stock_option_standby_reason = "All liquid F&O scrips are consolidating inside range bands. No breakout active."
        
        # -------------------------------------------------------------
        # 2. INTRADAY CASH: 750 BROAD MARKET CROWN HERO FILTER (SCORE >= 80)
        # Must be: High turnover (> ₹15 Cr), anti-circuit safe, score >= 80 + APPROVED by App #1
        # -------------------------------------------------------------
        approved_cash_stocks = [
            r for r in scan_results 
            if r.get("category") == "INTRADAY_CASH" 
            and r.get("signal_direction", 0) != 0
            and r.get("confluence_score", 0) >= 80
            and "APPROVED" in r.get("regime_confluence", "")  # MUST PASS MARKET CONDITIONS TEST
            and r.get("anti_circuit_status") == "SAFE"
        ]
        
        # Sort by highest confluence score
        approved_cash_stocks.sort(key=lambda x: x.get("confluence_score", 0), reverse=True)
        best_intraday_stock = approved_cash_stocks[0] if approved_cash_stocks else None
        
        # Find top cash candidate on watch if gated by score < 80 or market conditions
        coiled_cash_stocks = [
            r for r in scan_results 
            if r.get("category") == "INTRADAY_CASH" 
            and r.get("signal_direction", 0) != 0
            and r.get("anti_circuit_status") == "SAFE"
        ]
        coiled_cash_stocks.sort(key=lambda x: x.get("confluence_score", 0), reverse=True)
        top_watch_cash = coiled_cash_stocks[0] if coiled_cash_stocks else None
        
        if top_watch_cash:
            top_score = top_watch_cash.get('confluence_score', 0)
            if top_score < 80:
                cash_standby_reason = f"Capital Safeguard Active: Top candidate {top_watch_cash['symbol']} scored {top_score}/100 (below strict 80/100 threshold). HOLD CASH to protect capital."
            else:
                cash_standby_reason = f"Market Choppy: Top setup {top_watch_cash['symbol']} scored {top_score}/100, but index heavyweights ({heavyweights} above VWAP) lack thrust. Holding cash to avoid false intraday breakout."
        else:
            cash_standby_reason = "All 750 broad-market equities are inside range bands. Awaiting fresh breakout."
            
        # Detect Trend Continuation vs Previous 30m Cycle for Stock Option
        stock_continuation = False
        prev_stk = self.previous_selection.get("best_stock_option")
        if prev_stk and best_stock_option:
            if prev_stk.get("symbol") == best_stock_option.get("symbol") and prev_stk.get("signal_direction") == best_stock_option.get("signal_direction"):
                stock_continuation = True
                best_stock_option["is_continuation"] = True

        # Detect Trend Continuation vs Previous 30m Cycle for Intraday Cash Stock
        cash_continuation = False
        prev_cash = self.previous_selection.get("best_intraday_stock")
        if prev_cash and best_intraday_stock:
            if prev_cash.get("symbol") == best_intraday_stock.get("symbol") and prev_cash.get("signal_direction") == best_intraday_stock.get("signal_direction"):
                cash_continuation = True
                best_intraday_stock["is_continuation"] = True
                
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
            "top_watch_option": top_watch_option,
            "stock_option_standby_reason": stock_option_standby_reason,
            "top_watch_cash": top_watch_cash,
            "cash_standby_reason": cash_standby_reason,
            "market_diagnostic": {
                "status": "CHOPPY_STANDBY" if "VETOED" in sensex_radar.get("regime_confluence", "") else "ACTIVE",
                "trend_score": trend_score,
                "trend_label": trend_label,
                "heavyweights": heavyweights
            },
            "has_actionable_trades": (
                best_stock_option is not None or 
                best_intraday_stock is not None or 
                best_index_option.get("is_actionable", False)
            ),
            "index_continuation": index_continuation,
            "stock_continuation": stock_continuation,
            "cash_continuation": cash_continuation
        }

        self.previous_selection = self.current_selection if self.current_selection else selection_payload
        self.current_selection = selection_payload
        self.last_shortlist_utc = selection_payload["timestamp_utc"]
        return selection_payload

    def _format_index_radar(self, item: Optional[Dict[str, Any]], default_sym: str, default_lot: int, default_step: int, regime: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Formats Index Options Radar with STRICT SINGLE-DIRECTION RULE:
        - NEVER suggests Call (CE) and Put (PE) simultaneously.
        - If market is in Bullish Breakout + Uptrend: Suggests ONLY the Call Strike.
        - If market is in Bearish Breakdown + Downtrend: Suggests ONLY the Put Strike.
        - If Inside Range or Choppy/Vetoed: Suggests ZERO strikes with HOLD CASH.
        """
        if not item:
            return {
                "symbol": default_sym,
                "name": default_sym,
                "lot_size": default_lot,
                "last_price": 0.0,
                "pct_change_pdc": 0.0,
                "status": "INITIALIZING",
                "recommended_strike": None,
                "call_strike": None,
                "put_strike": None,
                "trade_side": "NONE_HOLD_CASH",
                "action_badge": "HOLD CASH / STANDBY",
                "directive": "Downloading 15m candle history...",
                "is_actionable": False
            }
            
        last_price = item.get("last_price", 0.0)
        strike_step = item.get("strike_step", default_step)
        atm_strike = int(round(last_price / strike_step) * strike_step) if last_price > 0 else 0
        
        sig_dir = item.get("signal_direction", 0)
        is_vetoed = "VETOED" in item.get("regime_confluence", "")
        confluence_badge = item.get("regime_badge", "")
        
        regime_ctx = regime or {}
        trend = regime_ctx.get("trend", {})
        trend_score = trend.get("score", 0)
        heavyweights = regime_ctx.get("metrics", {}).get("heavyweights_above", "0/5")
        
        recommended_strike = None
        call_strike = None
        put_strike = None
        trade_side = "NONE_HOLD_CASH"
        is_actionable = False
        action_badge = "HOLD CASH / STANDBY"
        directive = f"Inside Range ({item.get('range_bottom', 0)} - {item.get('range_top', 0)}). No option entry recommended."

        # Case 1: Bullish Breakout
        if sig_dir == 1:
            if is_vetoed:
                action_badge = "⏸️ STANDBY (MARKET CHOPPY)"
                directive = f"Breakout on Standby: Market is Choppy / Consolidating (CTI Score: +{trend_score}/100, Heavyweights: {heavyweights} above VWAP). Holding cash to prevent false breakout whipsaws and theta decay."
            else:
                call_strike = f"{item['symbol']} {atm_strike} CE"
                recommended_strike = call_strike
                trade_side = "CALL_ONLY"
                action_badge = f"🟢 BUY {call_strike} (CALL)"
                directive = f"Breakout above Range Top (₹{item.get('range_top')}) confirmed by Market Breadth. Target: +180%."
                is_actionable = True

        # Case 2: Bearish Breakdown
        elif sig_dir == -1:
            if is_vetoed:
                action_badge = "⏸️ STANDBY (MARKET CHOPPY)"
                directive = f"Breakdown on Standby: Market is Choppy / Consolidating (CTI Score: {trend_score}/100). Holding cash to protect capital."
            else:
                put_strike = f"{item['symbol']} {atm_strike} PE"
                recommended_strike = put_strike
                trade_side = "PUT_ONLY"
                action_badge = f"🔴 BUY {put_strike} (PUT)"
                directive = f"Breakdown below Range Bottom (₹{item.get('range_bottom')}) confirmed by Market Breadth. Target: +180%."
                is_actionable = True

        # Case 3: Inside Range / No Breakout
        else:
            action_badge = "HOLD CASH / INSIDE RANGE"
            directive = f"Price is inside consolidation range (₹{item.get('range_bottom')} - ₹{item.get('range_top')}). Zero options recommended."

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
            "vwap": item.get("vwap", 0.0),
            "ema50": item.get("ema50", 0.0),
            "ema50_dist_pct": item.get("ema50_dist_pct", 0.0),
            "signal_direction": sig_dir,
            "recommended_strike": recommended_strike,
            "call_strike": call_strike,
            "put_strike": put_strike,
            "trade_side": trade_side,
            "action_badge": action_badge,
            "directive": directive,
            "confluence_score": item.get("confluence_score", 0),
            "points_breakdown": item.get("points_breakdown", {}),
            "regime_confluence": item.get("regime_confluence", ""),
            "regime_badge": confluence_badge,
            "is_actionable": is_actionable
        }
