"""
FinPlus+ Best-in-Class Shortlist & Trend Continuation Engine
STRICT INSTITUTIONAL MARKET REGIME GATING:
1. Never suggest CE and PE simultaneously.
2. Market conditions (App #1 Air Traffic Control) MUST confirm the direction before any strike is recommended.
3. If market regime is Choppy, Neutral, or Vetoed, output HOLD CASH (Zero options recommended).
4. Stock Options and Intraday Cash are completely decoupled.
"""

from typing import Dict, Any, List, Optional
from src.utils.time_and_logger import setup_logger, get_utc_iso, get_ist_time_str, get_ist_now

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
        midcap_item = next((r for r in scan_results if r.get("symbol") in ("MIDCPNIFTY", "MIDCAP NIFTY")), None)
        
        # Format Sensex Radar (Never suggests CE and PE at the same time)
        sensex_radar = self._format_index_radar(sensex_item, default_sym="SENSEX", default_lot=20, default_step=100, regime=regime)
        
        # Format Nifty Radar (Never suggests CE and PE at the same time)
        nifty_radar = self._format_index_radar(nifty_item, default_sym="NIFTY", default_lot=65, default_step=50, regime=regime)
        
        # Format Midcap Nifty Radar (Never suggests CE and PE at the same time)
        midcap_radar = self._format_index_radar(midcap_item, default_sym="MIDCPNIFTY", default_lot=75, default_step=25, regime=regime)
        
        # -------------------------------------------------------------
        # MIDDAY THETA FREEZE GATE (11:30 AM to 01:00 PM IST)
        # Empirical rule: 11:30 - 13:00 is a dead-zone with 0% win-rate & high theta decay.
        # Zero fresh trade signals are permitted during this window.
        # -------------------------------------------------------------
        now_ist = get_ist_now()
        hour_float = now_ist.hour + (now_ist.minute / 60.0)
        is_midday_freeze = (11.5 <= hour_float < 13.0)

        # -------------------------------------------------------------
        # 1. STOCK OPTIONS: STRICT MARKET CONDITION FILTER
        # Must be: F&O (lot <= 500) + Score >= 50 + APPROVED by App #1 Macro Regime
        # -------------------------------------------------------------
        if is_midday_freeze:
            best_stock_option = None
            top_watch_option = None
            stock_option_standby_reason = "Midday Theta Freeze Active (11:30 - 13:00 IST): Stock option buying paused to eliminate lunchtime time-decay & low-volume chop. Monitoring bases for 13:00+ European open."
        else:
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
        # INSTITUTIONAL LAUNCHPAD LOGIC:
        # - Prioritizes Tight Base Coiling near Resistance (PRE_IGNITION_LONG)
        # - Supports High-Momentum Continuations (STAGE2_CONTINUATION_LAUNCHPAD, e.g. NETWEB)
        # - Never bars a stock simply because it grew, as long as it has formed a tight base above VWAP
        # -------------------------------------------------------------
        from src.analytics.multi_horizon_engine import MultiHorizonScorer
        
        # Calculate Multi-Horizon Intraday VCP & Orderflow score for all cash candidates
        for r in scan_results:
            if r.get("category") == "INTRADAY_CASH":
                try:
                    vcp_eval = MultiHorizonScorer.score_intraday_vcp_orderflow(r, nifty_change_pct=trend_score)
                    r["vcp_score"] = vcp_eval.get("score", 0)
                    r["is_launchpad"] = vcp_eval.get("is_launchpad", False)
                    r["vcp_reasons"] = vcp_eval.get("reasons", [])
                    if vcp_eval.get("trigger_price"):
                        r["trigger_price"] = vcp_eval.get("trigger_price")
                    if vcp_eval.get("tight_sl"):
                        r["tight_sl"] = vcp_eval.get("tight_sl")
                    if vcp_eval.get("target_price"):
                        r["target_price"] = vcp_eval.get("target_price")
                    r["base_high"] = vcp_eval.get("base_high")
                    r["base_low"] = vcp_eval.get("base_low")
                    r["base_width_pct"] = vcp_eval.get("base_width_pct")
                    r["dist_to_pivot_pct"] = vcp_eval.get("dist_to_pivot_pct")
                except Exception:
                    pass

        valid_long_states = (
            "PRE_IGNITION_LONG", 
            "STAGE2_CONTINUATION_LAUNCHPAD", 
            "FRESH_BREAKOUT", 
            "BULLISH_IGNITION"
        )
        valid_short_states = (
            "PRE_IGNITION_SHORT",
            "BEARISH_IGNITION",
            "RANGE_BREAKDOWN_SHORT",
            "MILD_BREAKDOWN_SHORT",
            "STAGE2_DISTRIBUTION_SHORT"
        )
        valid_launchpad_states = valid_long_states + valid_short_states

        if is_midday_freeze:
            approved_cash_stocks = []
            best_intraday_stock = None
            top_intraday_stocks = []
            top_watch_cash = None
            top_watch_stocks = []
            cash_standby_reason = "Midday Freeze Active (11:30 - 13:00 IST): Trading paused to eliminate lunchtime chop. Monitoring bases for 13:00+ European open."
            sensex_radar["is_actionable"] = False
            nifty_radar["is_actionable"] = False
            midcap_radar["is_actionable"] = False
            sensex_radar["directive"] = "Midday Theta Freeze Active (11:30 - 13:00 IST). Zero options recommended."
            nifty_radar["directive"] = "Midday Theta Freeze Active (11:30 - 13:00 IST). Zero options recommended."
            midcap_radar["directive"] = "Midday Theta Freeze Active (11:30 - 13:00 IST). Zero options recommended."
        else:
            approved_cash_stocks = [
                r for r in scan_results 
                if r.get("category") == "INTRADAY_CASH" 
                and r.get("signal_direction", 0) != 0
                and r.get("confluence_score", 0) >= 80
                and "APPROVED" in r.get("regime_confluence", "")  # MUST PASS MARKET CONDITIONS TEST
                and r.get("anti_circuit_status") == "SAFE"
                and r.get("ignition_state") in valid_launchpad_states
            ]
            
            # Sort by highest combined Confluence + VCP Launchpad Score
            approved_cash_stocks.sort(
                key=lambda x: (x.get("confluence_score", 0) + (15 if x.get("is_launchpad") else 0)),
                reverse=True
            )
            best_intraday_stock = approved_cash_stocks[0] if approved_cash_stocks else None
            top_intraday_stocks = approved_cash_stocks[:3]
            
            # Find top cash candidate on watch (un-extended coiling scrips only)
            coiled_cash_stocks = [
                r for r in scan_results 
                if r.get("category") == "INTRADAY_CASH" 
                and r.get("signal_direction", 0) != 0
                and r.get("anti_circuit_status") == "SAFE"
                and r.get("ignition_state") in valid_launchpad_states
            ]
            coiled_cash_stocks.sort(
                key=lambda x: (x.get("confluence_score", 0) + (15 if x.get("is_launchpad") else 0)),
                reverse=True
            )
            top_watch_cash = coiled_cash_stocks[0] if coiled_cash_stocks else None
            top_watch_stocks = coiled_cash_stocks[:3]

            # Do NOT force an unapproved stock to be qualified buy/sell; if not approved, keep as watchlist standby
            if not best_intraday_stock and top_watch_cash:
                # Candidate is on watch/coiling, NOT an active signal
                best_intraday_stock = dict(top_watch_cash)
                best_intraday_stock["is_standby_watch"] = True
                is_watch_short = int(best_intraday_stock.get("signal_direction", 0)) == -1
                best_intraday_stock["intraday_action_badge"] = (
                    "⚡ ON WATCH (COILING AT BREAKDOWN FLOOR)" if is_watch_short else "⚡ ON WATCH (COILING AT LAUNCHPAD)"
                )
                top_intraday_stocks = coiled_cash_stocks[:3]
            
            if top_watch_cash:
                top_score = top_watch_cash.get('confluence_score', 0)
                is_top_short = int(top_watch_cash.get("signal_direction", 0)) == -1
                if top_score < 80:
                    cash_standby_reason = f"Capital Safeguard Active: Top {'short' if is_top_short else 'buy'} candidate {top_watch_cash['symbol']} scored {top_score}/100 (below strict 80/100 threshold). HOLD CASH to protect capital."
                else:
                    cash_standby_reason = f"Market Choppy: Top {'short' if is_top_short else 'buy'} setup {top_watch_cash['symbol']} scored {top_score}/100, but index momentum lacks directional thrust. Holding cash to avoid false intraday {'breakdown' if is_top_short else 'breakout'}."
            else:
                cash_standby_reason = "All 750 broad-market equities are inside range bands. Awaiting fresh breakout or breakdown."
            
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

        # -------------------------------------------------------------
        # 3. MULTI-HORIZON SELECTIONS (INTRADAY, SWING, LONG-TERM, PENNY)
        # -------------------------------------------------------------
        from src.analytics.multi_horizon_engine import MultiHorizonScorer
        
        # A. SWING TRADING SELECTION (Stage-2 Mark Minervini Trend)
        swing_candidates = []
        for r in scan_results:
            if r.get("category") == "INTRADAY_CASH" and r.get("last_price", 0) >= 50.0:
                s_eval = MultiHorizonScorer.score_swing_stage2(r, r)
                if s_eval["score"] >= 65:
                    item_copy = dict(r)
                    item_copy["horizon_score"] = s_eval["score"]
                    item_copy["horizon_reasons"] = s_eval["reasons"]
                    swing_candidates.append(item_copy)
        swing_candidates.sort(key=lambda x: x["horizon_score"], reverse=True)
        best_swing_stock = swing_candidates[0] if swing_candidates else None

        # B. LONG-TERM WEALTH COMPOUNDER SELECTION (Durability + Valuation)
        lt_candidates = []
        for r in scan_results:
            if r.get("category") == "INTRADAY_CASH":
                lt_eval = MultiHorizonScorer.score_long_term_compounder(r)
                if lt_eval["score"] >= 70:
                    item_copy = dict(r)
                    item_copy["horizon_score"] = lt_eval["score"]
                    item_copy["horizon_reasons"] = lt_eval["reasons"]
                    lt_candidates.append(item_copy)
        lt_candidates.sort(key=lambda x: x["horizon_score"], reverse=True)
        best_longterm_stock = lt_candidates[0] if lt_candidates else None

        # C. PENNY / HIGH-BETA RUNNER SELECTION (< Rs 120 + High RVOL)
        penny_candidates = []
        for r in scan_results:
            if r.get("category") == "INTRADAY_CASH":
                p_eval = MultiHorizonScorer.score_penny_runner(r, r)
                if p_eval["score"] >= 50:
                    item_copy = dict(r)
                    item_copy["horizon_score"] = p_eval["score"]
                    item_copy["horizon_reasons"] = p_eval["reasons"]
                    penny_candidates.append(item_copy)
        penny_candidates.sort(key=lambda x: x["horizon_score"], reverse=True)
        best_penny_stock = penny_candidates[0] if penny_candidates else None

        selection_payload = {
            "timestamp_utc": get_utc_iso(),
            "ist_time": get_ist_time_str(),
            "sensex_radar": sensex_radar,
            "nifty_radar": nifty_radar,
            "midcap_radar": midcap_radar,
            "best_index_option": best_index_option,
            "best_stock_option": best_stock_option,
            "best_intraday_stock": best_intraday_stock,
            "best_swing_stock": best_swing_stock,
            "best_longterm_stock": best_longterm_stock,
            "best_penny_stock": best_penny_stock,
            "top_intraday_stocks": top_intraday_stocks,
            "top_watch_option": top_watch_option,
            "stock_option_standby_reason": stock_option_standby_reason,
            "top_watch_cash": top_watch_cash,
            "top_watch_stocks": top_watch_stocks,
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
            
        regime_ctx = regime or {}
        if default_sym == "NIFTY" and regime_ctx.get("metrics", {}).get("nifty_spot"):
            last_price = float(regime_ctx.get("metrics", {}).get("nifty_spot"))
        else:
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
