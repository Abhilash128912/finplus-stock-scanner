"""
FinPlus+ LuxAlgo Range Detector & Key Levels Engine
Generates explicit actionable directives for:
1. Stock Options: BUY CE (Call) vs BUY PE (Put)
2. Intraday Cash: BUY / LONG vs SELL / SHORT with exact SL and Target price levels
3. Index Options: Generates both Call (CE) and Put (PE) strikes for Sensex and Nifty
"""

import math
import numpy as np
import pandas as pd
from typing import Dict, Any, List, Optional
from datetime import datetime, timezone

from src.utils.time_and_logger import get_utc_iso, setup_logger

logger = setup_logger("RangeDetectorEngine")

def calc_rma(series: pd.Series, length: int) -> pd.Series:
    alpha = 1.0 / length
    return series.ewm(alpha=alpha, adjust=False).mean()

def calc_atr(df: pd.DataFrame, length: int = 500) -> pd.Series:
    high = df['High']
    low = df['Low']
    close = df['Close']
    prev_close = close.shift(1)
    
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    
    eff_len = min(length, max(14, len(df) // 2))
    return calc_rma(tr, eff_len)

def calc_intraday_vwap(df: pd.DataFrame) -> pd.Series:
    typical_price = (df['High'] + df['Low'] + df['Close']) / 3.0
    vol = df['Volume'].replace(0, 1)
    dates = pd.to_datetime(df.index).date
    tp_vol = typical_price * vol
    cum_tp_vol = tp_vol.groupby(dates).cumsum()
    cum_vol = vol.groupby(dates).cumsum()
    return cum_tp_vol / cum_vol

class RangeDetectorEngine:
    def __init__(self, length: int = 20, mult: float = 1.0, atr_len: int = 500):
        self.length = length
        self.mult = mult
        self.atr_len = atr_len

    def evaluate_instrument(self, raw_df: pd.DataFrame, meta: Dict[str, Any]) -> Dict[str, Any]:
        symbol = meta.get("symbol", "UNKNOWN")
        if raw_df.empty or len(raw_df) < self.length + 5:
            return {
                "symbol": symbol,
                "status": "INSUFFICIENT_DATA",
                "timestamp_utc": get_utc_iso()
            }

        # Market Holiday & Freshness Gate: Prevent stale yesterday candles from triggering on holidays
        try:
            from src.utils.indian_market_calendar import get_current_ist_datetime, is_nse_holiday
            today_date = get_current_ist_datetime().date()
            last_candle_date = pd.to_datetime(raw_df.index[-1]).date()
            is_hol, hol_name = is_nse_holiday(today_date)

            if is_hol or last_candle_date < today_date:
                status_desc = f"Exchange Closed ({hol_name})" if is_hol else "Market Closed / Awaiting First Live Candle"
                last_price = float(raw_df['Close'].iloc[-1])
                return {
                    "symbol": symbol,
                    "category": meta.get("category", "STOCK"),
                    "status": "MARKET_CLOSED_HOLIDAY",
                    "signal_direction": 0,
                    "confluence_score": 0,
                    "ignition_state": "MARKET_CLOSED",
                    "tier_label": "STANDBY (MARKET CLOSED)",
                    "reason": status_desc,
                    "last_price": round(last_price, 2),
                    "timestamp_utc": get_utc_iso(),
                    "intraday_action_badge": "HOLD CASH",
                    "option_directive_badge": "MARKET CLOSED"
                }
        except Exception:
            pass
            
        df = raw_df.copy()
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
            
        # Closed candle slice
        closed_df = df.iloc[:-1].copy() if len(df) > 30 else df.copy()
        
        # Indicators
        closed_df['atr'] = calc_atr(closed_df, self.atr_len) * self.mult
        closed_df['sma'] = closed_df['Close'].rolling(self.length).mean()
        closed_df['vwap'] = calc_intraday_vwap(closed_df)
        closed_df['vol_ma20'] = closed_df['Volume'].rolling(20).mean()
        # 50 EMA Trend Gate
        closed_df['ema50'] = closed_df['Close'].ewm(span=50, adjust=False).mean()
        
        # Daily Key Levels
        dates = pd.to_datetime(closed_df.index).date
        daily = closed_df.groupby(dates).agg({'High': 'max', 'Low': 'min', 'Close': 'last'})
        if len(daily) < 2:
            pdh = float(closed_df['High'].max())
            pdl = float(closed_df['Low'].min())
            pdc = float(closed_df['Close'].iloc[0])
        else:
            prev_day = daily.iloc[-2]
            pdh = float(prev_day['High'])
            pdl = float(prev_day['Low'])
            pdc = float(prev_day['Close'])
            
        closes = closed_df['Close'].values
        volumes = closed_df['Volume'].values
        vol_ma = closed_df['vol_ma20'].values
        atr_vals = closed_df['atr'].values
        sma_vals = closed_df['sma'].values
        vwap_vals = closed_df['vwap'].values
        
        n_bars = len(closed_df)
        bx_right = -1
        bx_top = np.nan
        bx_bottom = np.nan
        os_state = 0
        prev_count = 1
        is_coiling = False
        
        for n in range(self.length, n_bars):
            curr_ma = sma_vals[n]
            curr_atr = atr_vals[n]
            if np.isnan(curr_ma) or np.isnan(curr_atr):
                continue
                
            count = 0
            for i in range(self.length):
                if abs(closes[n - i] - curr_ma) > curr_atr:
                    count += 1
                    
            if count == 0 and prev_count != 0:
                if (n - self.length) <= bx_right and not np.isnan(bx_top):
                    bx_top = max(curr_ma + curr_atr, bx_top)
                    bx_bottom = min(curr_ma - curr_atr, bx_bottom)
                    bx_right = n
                else:
                    bx_right = n
                    bx_top = curr_ma + curr_atr
                    bx_bottom = curr_ma - curr_atr
                    os_state = 0
            elif count == 0:
                bx_right = n
                
            prev_count = count
            
            if not np.isnan(bx_top) and n <= bx_right + 5:
                if os_state == 0:
                    if closes[n] > bx_top:
                        os_state = 1
                    elif closes[n] < bx_bottom:
                        os_state = -1
                elif os_state == 1 and closes[n] < bx_bottom:
                    os_state = -1
                elif os_state == -1 and closes[n] > bx_top:
                    os_state = 1
            else:
                os_state = 0
                
            if n == n_bars - 1:
                is_coiling = (count == 0)

        last_closed_price = float(closes[-1])
        last_vwap = float(vwap_vals[-1]) if not np.isnan(vwap_vals[-1]) else last_closed_price
        last_vol = float(volumes[-1])
        last_vma = float(vol_ma[-1]) if not np.isnan(vol_ma[-1]) else 1.0
        rvol = round(last_vol / max(1.0, last_vma), 2)
        pct_change_pdc = round(((last_closed_price - pdc) / pdc) * 100.0, 2)
        
        range_top_val = float(bx_top) if not np.isnan(bx_top) else (last_closed_price + (last_closed_price * 0.005))
        range_bot_val = float(bx_bottom) if not np.isnan(bx_bottom) else (last_closed_price - (last_closed_price * 0.005))
        range_mid_val = (range_top_val + range_bot_val) / 2.0
        
        # -------------------------------------------------------------
        # Institutional Micro-Base & Resistance Pivot Analysis (Last 3 to 6 bars)
        # Identifies tight consolidation bases (< 1.2% width) near HOD/Resistance
        # Enables Early Pre-Breakout Detection and Stage-2 Continuations (e.g. NETWEB)
        # -------------------------------------------------------------
        recent_bars = closed_df.iloc[-min(6, len(closed_df)):]
        mb_high = float(recent_bars['High'].max())
        mb_low = float(recent_bars['Low'].min())
        mb_width_pct = round(((mb_high - mb_low) / max(1.0, mb_high)) * 100.0, 2)
        
        # If micro-base is tight (<= 1.2%), use it as the high-fidelity institutional breakout level
        if mb_width_pct <= 1.2 and mb_high > 0:
            effective_resistance = mb_high
            effective_support = mb_low
            has_tight_base = True
        else:
            effective_resistance = range_top_val
            effective_support = range_bot_val
            has_tight_base = False

        # 50 EMA Trend Confirmation & Proximity Metrics
        ema50_vals = closed_df['ema50'].values
        last_ema50 = float(ema50_vals[-1]) if not np.isnan(ema50_vals[-1]) else last_closed_price
        ema50_dist_pct = round(((last_closed_price - last_ema50) / last_ema50) * 100.0, 2)
        
        is_index = (meta.get("category") == "INDEX_OPTIONS")
        max_dist_pct = 2.0 if is_index else 3.5  # Max safe proximity from 50 EMA to avoid chasing overextended moves
        
        above_ema50 = last_closed_price > last_ema50
        below_ema50 = last_closed_price < last_ema50
        ema50_near_long = (0.0 < ema50_dist_pct <= max_dist_pct)
        ema50_near_short = (-max_dist_pct <= ema50_dist_pct < 0.0)

        # Confluence Scoring (Total: 100 pts)
        pts_range = 0
        pts_ema50 = 0
        pts_pdh_pdl = 0
        pts_vwap = 0
        pts_volume = 0
        signal_direction = 0
        ignition_state = "NEUTRAL"
        tier_label = "CONSOLIDATION"
        
        broke_top = last_closed_price > effective_resistance
        broke_bot = last_closed_price < effective_support
        above_pdh = last_closed_price > pdh
        below_pdl = last_closed_price < pdl
        above_vwap = last_closed_price > last_vwap
        below_vwap = last_closed_price < last_vwap
        has_volume = rvol >= 1.25
        
        # Early Breakout / Coiling Proximity Calculation
        # Distance to Resistance: positive means still below pivot (pre-ignition coiling), negative means broke above
        dist_to_top_pct = round(((effective_resistance - last_closed_price) / effective_resistance) * 100.0, 2)
        dist_to_bot_pct = round(((last_closed_price - effective_support) / effective_support) * 100.0, 2)
        
        # Pre-Ignition Launchpad: Price coiled within 0.0% to 0.7% of Resistance, above 50 EMA and Day VWAP
        is_pre_ignition_long = (0.0 <= dist_to_top_pct <= 0.7) and above_ema50 and above_vwap and has_tight_base
        is_pre_ignition_short = (0.0 <= dist_to_bot_pct <= 0.7) and below_ema50 and below_vwap and has_tight_base

        if is_pre_ignition_long:
            signal_direction = 1
            pts_range = 35 # Coiled at immediate breakout edge
            pts_ema50 = 30 # Trend aligned
            pts_vwap = 20  # Buyers in control
            pts_volume = 15 if rvol >= 1.25 else 10
            confluence_score = pts_range + pts_ema50 + pts_vwap + pts_volume
            
            # High-Momentum Continuation Handling (e.g., NETWEB, TRENT): Never bar runners that form a tight base
            if pct_change_pdc >= 2.5:
                ignition_state = "STAGE2_CONTINUATION_LAUNCHPAD"
                tier_label = "⚡ HIGH-TIGHT BASE (STAGE-2 CONTINUATION)"
                reason = f"High-Tight Continuation Base: Stock (+{pct_change_pdc:+.2f}%) consolidated in tight {mb_width_pct}% base above VWAP. Coiled {dist_to_top_pct}% below pivot ₹{effective_resistance:.2f}."
            else:
                ignition_state = "PRE_IGNITION_LONG"
                tier_label = "⚡ PRE-IGNITION LAUNCHPAD (EARLY ENTRY)"
                reason = f"Coiled at Breakout Edge: Price (₹{last_closed_price:.2f}) is only {dist_to_top_pct}% below Resistance (₹{effective_resistance:.2f}) in tight {mb_width_pct}% base."

        elif is_pre_ignition_short:
            signal_direction = -1
            pts_range = 35
            pts_ema50 = 30
            pts_vwap = 20
            pts_volume = 15 if rvol >= 1.25 else 10
            confluence_score = pts_range + pts_ema50 + pts_vwap + pts_volume
            ignition_state = "PRE_IGNITION_SHORT"
            tier_label = "⚡ PRE-IGNITION LAUNCHPAD (EARLY SHORT)"
            reason = f"Coiled at Breakdown Edge: Price (₹{last_closed_price:.2f}) is only {dist_to_bot_pct}% above Support (₹{effective_support:.2f}) with downward momentum."

        elif is_coiling:
            ignition_state = "COILING_ALERT"
            tier_label = "PRE-IGNITION SQUEEZE"
            reason = "Spring coiled: 20 closed candles compressed within ±1 ATR band."
            
        elif broke_top:
            breakout_dist_pct = round(((last_closed_price - effective_resistance) / effective_resistance) * 100.0, 2)
            if not above_ema50:
                signal_direction = 0
                ignition_state = "REJECTED_BELOW_EMA50"
                tier_label = "DISQUALIFIED (BELOW 50 EMA)"
                reason = f"Uptrend unconfirmed: Price (₹{last_closed_price:.2f}) is below 15m 50 EMA (₹{last_ema50:.2f}). Longs disqualified."
            elif not ema50_near_long:
                signal_direction = 0
                ignition_state = "OVEREXTENDED_FROM_EMA50"
                tier_label = "OVEREXTENDED (> 50 EMA)"
                reason = f"Breakout overextended: Price is +{ema50_dist_pct}% above 50 EMA (Max safe: +{max_dist_pct}%). Mean reversion risk high."
            elif breakout_dist_pct > 1.2 and not has_tight_base:
                is_cash_equity = (meta.get("category") == "INTRADAY_CASH" or meta.get("capital_tier") == "EQUITY_CASH")
                is_cash_momentum = (is_cash_equity and (rvol >= 2.0 or (pct_change_pdc >= 2.5 and rvol >= 1.4)) and breakout_dist_pct <= 4.5)
                if is_cash_momentum:
                    # High-Velocity Cash Momentum Runner (e.g. ZEEL, COCHINSHIP, NETWEB)
                    signal_direction = 1
                    pts_range = 30
                    pts_ema50 = 25
                    pts_vwap = 20 if above_vwap else 10
                    pts_volume = 20  # Max volume reward
                    total_pts = pts_range + pts_ema50 + pts_vwap + pts_volume
                    confluence_score = total_pts
                    ignition_state = "STAGE2_MOMENTUM_RUNNER"
                    tier_label = "🚀 HIGH-MOMENTUM CASH RUNNER (EXPLOSIVE RVOL)"
                    reason = f"High-Velocity Cash Breakout above ₹{effective_resistance:.2f} [+{breakout_dist_pct}%] on {rvol:.1f}x Explosive RVOL. Tight SL at ₹{effective_resistance:.2f}."
                else:
                    # Mid-air spike without base: Wait for consolidation
                    signal_direction = 0
                    confluence_score = 50
                    ignition_state = "AWAITING_BASE_FORMATION"
                    tier_label = "WAIT FOR BASE / VWAP PULLBACK"
                    reason = f"Impulse active @ ₹{last_closed_price:.2f}: Stretched +{breakout_dist_pct}% above pivot without consolidation. Wait for 3-bar base near VWAP."
            else:
                signal_direction = 1
                pts_range = 35  # Fresh breakout through resistance
                pts_ema50 = 30
                if above_vwap:
                    pts_vwap = 20
                if rvol >= 1.25:
                    pts_volume = 15
                elif rvol >= 1.0:
                    pts_volume = 10
                    
                total_pts = pts_range + pts_ema50 + pts_vwap + pts_volume
                confluence_score = total_pts
                if total_pts >= 85:
                    ignition_state = "BULLISH_IGNITION"
                    tier_label = "TIER-1 HIGH CONVICTION"
                    reason = f"Fresh Breakout above ₹{effective_resistance:.2f} [+{breakout_dist_pct}%] + 50 EMA confirmed (+{ema50_dist_pct}%) + VWAP confluence"
                elif total_pts >= 65:
                    ignition_state = "RANGE_BREAKOUT_LONG"
                    tier_label = "TIER-2 EARLY SCALP"
                    reason = f"Closed above Resistance with 50 EMA (+{ema50_dist_pct}%) & VWAP confluence"
                else:
                    ignition_state = "MILD_BREAKOUT_LONG"
                    tier_label = "WATCHLIST ONLY"
                    reason = f"Breakout with weak volume support"

        elif broke_bot:
            breakdown_dist_pct = round(((range_bot_val - last_closed_price) / range_bot_val) * 100.0, 2)
            # DOWNTREND CONFIRMATION GATE: Must be below 50 EMA (and not so far)
            if not below_ema50:
                signal_direction = 0
                ignition_state = "REJECTED_ABOVE_EMA50"
                tier_label = "DISQUALIFIED (ABOVE 50 EMA)"
                reason = f"Downtrend unconfirmed: Price (₹{last_closed_price:.2f}) is above 15m 50 EMA (₹{last_ema50:.2f}). Shorts disqualified."
            elif not ema50_near_short:
                signal_direction = 0
                ignition_state = "OVEREXTENDED_BELOW_EMA50"
                tier_label = "OVEREXTENDED (< 50 EMA)"
                reason = f"Breakdown overextended: Price is {ema50_dist_pct}% below 50 EMA (Max safe: -{max_dist_pct}%). Oversold bounce risk."
            elif breakdown_dist_pct > 1.2 or (pct_change_pdc <= -2.5 and breakdown_dist_pct > 0.6):
                is_cash_equity = (meta.get("category") == "INTRADAY_CASH" or meta.get("capital_tier") == "EQUITY_CASH")
                is_cash_breakdown = (is_cash_equity and (rvol >= 2.0 or (pct_change_pdc <= -2.5 and rvol >= 1.4)) and breakdown_dist_pct <= 4.5)
                if is_cash_breakdown:
                    signal_direction = -1
                    pts_range = 30
                    pts_ema50 = 25
                    pts_vwap = 20 if below_vwap else 10
                    pts_volume = 20
                    total_pts = pts_range + pts_ema50 + pts_vwap + pts_volume
                    confluence_score = total_pts
                    ignition_state = "STAGE2_MOMENTUM_BREAKDOWN"
                    tier_label = "⚡ HIGH-MOMENTUM CASH BREAKDOWN (EXPLOSIVE RVOL)"
                    reason = f"High-Velocity Cash Breakdown below ₹{range_bot_val:.2f} [-{breakdown_dist_pct}%] on {rvol:.1f}x Volume. Trailing SL at ₹{range_bot_val:.2f}."
                else:
                    signal_direction = 0
                    confluence_score = 45
                    ignition_state = "OVEREXTENDED_SHORT"
                    tier_label = "⚠️ EXTENDED (DO NOT CHASE)"
                    reason = f"Overextended breakdown: Price (₹{last_closed_price:.2f}) is -{breakdown_dist_pct}% past Breakdown (₹{range_bot_val:.2f}) & {pct_change_pdc:+.2f}% from PDC. DO NOT CHASE. Wait for bounce to VWAP (₹{last_vwap:.2f})."
            else:
                signal_direction = -1
                pts_range = 35  # Range Bottom breakdown (fresh within 0.0% - 1.2% of pivot)
                pts_ema50 = 30  # Confirmed below 50 EMA within safe proximity
                if below_vwap:
                    pts_vwap = 20
                if rvol >= 1.25:
                    pts_volume = 15
                elif rvol >= 1.0:
                    pts_volume = 10
                    
                total_pts = pts_range + pts_ema50 + pts_vwap + pts_volume
                confluence_score = total_pts
                if total_pts >= 85:
                    ignition_state = "BEARISH_IGNITION"
                    tier_label = "TIER-1 HIGH CONVICTION"
                    reason = f"Closed below Range Bottom (₹{range_bot_val:.2f}) [-{breakdown_dist_pct}%] + 50 EMA confirmed ({ema50_dist_pct}%) - VWAP confluence"
                elif total_pts >= 65:
                    ignition_state = "RANGE_BREAKDOWN_SHORT"
                    tier_label = "TIER-2 EARLY SCALP"
                    reason = f"Closed below Range Bottom with 50 EMA ({ema50_dist_pct}%) - VWAP confluence"
                else:
                    ignition_state = "MILD_BREAKDOWN_SHORT"
                    tier_label = "WATCHLIST ONLY"
                    reason = f"Range breakdown with weak volume support"
        else:
            reason = "Inside Range / Normal Consolidation"

        points_breakdown = {
            "range_breakout_pts": pts_range,
            "ema50_pts": pts_ema50,
            "vwap_pts": pts_vwap,
            "volume_pts": pts_volume,
            "total_score": pts_range + pts_ema50 + pts_vwap + pts_volume
        }
        confluence_score = points_breakdown["total_score"]

        # -------------------------------------------------------------
        # EXPLICIT ACTION DIRECTIVES (CE / PE vs INTRADAY CASH BUY / SELL)
        # -------------------------------------------------------------
        category = meta.get("category", "STOCK_OPTIONS")
        strike_step = meta.get("strike_step", 50)
        atm_strike = int(round(last_closed_price / strike_step) * strike_step)
        
        is_cash_segment = (category == "INTRADAY_CASH")
        
        if is_cash_segment:
            call_strike_name = "-"
            put_strike_name = "-"
            option_action_badge = "EQUITY CASH ONLY"
            option_directive_text = "Cash equity setup (High momentum runner, not an F&O contract)."
        else:
            # Dual Contracts: Call (CE) & Put (PE) for F&O Stocks / Indices
            call_strike_name = f"{symbol} {atm_strike} CE"
            put_strike_name = f"{symbol} {atm_strike} PE"
            option_action_badge = "WAITING"
            option_directive_text = "No breakout active. Avoid options."

        intraday_action_badge = "SIT OUT"
        intraday_directive_text = "Price inside range. Avoid fresh cash entries."
        
        underlying_sl = 0.0
        underlying_target = 0.0
        recommended_contract = None
        risk_management = {}
        
        # Anti-circuit verification for cash equities
        anti_circuit_status = "SAFE"
        if is_cash_segment:
            if abs(pct_change_pdc) >= 9.5:
                anti_circuit_status = "EXTREME_CIRCUIT_DANGER"
            elif abs(pct_change_pdc) >= 4.75 and meta.get("turnover_cr", 100) < 5.0:
                anti_circuit_status = "CIRCUIT_RISK_WARNING"

        if signal_direction == 1: # Bullish
            if not is_cash_segment:
                option_action_badge = f"BUY {call_strike_name} (CALL)"
                recommended_contract = call_strike_name
            intraday_action_badge = f"BUY / LONG INTRADAY"
            
            underlying_sl = round(range_bot_val, 2)
            risk_amt = max(last_closed_price - underlying_sl, last_closed_price * 0.003)
            underlying_target = round(last_closed_price + (risk_amt * 2.0), 2)
            
            if ignition_state == "PRE_IGNITION_LONG":
                if not is_cash_segment:
                    option_directive_text = f"⚡ EARLY ATM CALL: BUY {call_strike_name} | Coiled at launchpad (Breakout Trigger: ₹{range_top_val:.2f}). Target ₹{underlying_target}."
                intraday_directive_text = f"⚡ EARLY ENTRY: BUY {symbol} @ ₹{last_closed_price:.2f} (Trigger: ₹{range_top_val:.2f}) | SL: ₹{underlying_sl:.2f} | Target: ₹{underlying_target:.2f} (1:2 R:R)"
            else:
                if not is_cash_segment:
                    option_directive_text = f"BUY {call_strike_name} | Spot > Range Top. Target underlying ₹{underlying_target}."
                intraday_directive_text = f"BUY {symbol} @ ₹{last_closed_price} | SL: ₹{underlying_sl} | Target: ₹{underlying_target} (1:2 R:R) | Exit by 15:15 IST"
            
            if category == "INDEX_OPTIONS":
                risk_management = {
                    "contract_type": "WEEKLY_CALL",
                    "action": f"BUY {call_strike_name}",
                    "premium_stop_loss_pct": meta.get("max_sl_premium_pct", 45.0),
                    "premium_target_pct": meta.get("target_gain_pct", 180.0),
                    "capital_guidance": "Small capital allocation. Max 45% premium SL, 180%+ Target."
                }
            elif category == "STOCK_OPTIONS":
                risk_management = {
                    "contract_type": "MONTHLY_CALL_ATM",
                    "action": f"BUY {call_strike_name}",
                    "underlying_entry": last_closed_price,
                    "underlying_sl": underlying_sl,
                    "underlying_target": underlying_target,
                    "premium_stop_loss_pct": 25.0,
                    "rr_ratio": "1:2.0"
                }
            else: # INTRADAY_CASH
                risk_management = {
                    "segment": "EQUITY_CASH",
                    "action": f"BUY {symbol} (CASH)",
                    "entry_price": last_closed_price,
                    "stop_loss": underlying_sl,
                    "target": underlying_target,
                    "rr_ratio": "1:2.0",
                    "anti_circuit": anti_circuit_status,
                    "square_off": "Mandatory square-off before 15:15 IST"
                }

        elif signal_direction == -1: # Bearish
            if not is_cash_segment:
                option_action_badge = f"BUY {put_strike_name} (PUT)"
                recommended_contract = put_strike_name
            intraday_action_badge = f"SELL / SHORT INTRADAY"
            
            underlying_sl = round(range_top_val, 2)
            risk_amt = max(underlying_sl - last_closed_price, last_closed_price * 0.003)
            underlying_target = round(last_closed_price - (risk_amt * 2.0), 2)
            
            if ignition_state == "PRE_IGNITION_SHORT":
                if not is_cash_segment:
                    option_directive_text = f"⚡ EARLY ATM PUT: BUY {put_strike_name} | Coiled at breakdown launchpad (Trigger: ₹{range_bot_val:.2f}). Target ₹{underlying_target}."
                intraday_directive_text = f"⚡ EARLY SHORT: SHORT {symbol} @ ₹{last_closed_price:.2f} (Trigger: ₹{range_bot_val:.2f}) | SL: ₹{underlying_sl:.2f} | Target: ₹{underlying_target:.2f} (1:2 R:R)"
            else:
                if not is_cash_segment:
                    option_directive_text = f"BUY {put_strike_name} | Spot < Range Bottom. Target underlying ₹{underlying_target}."
                intraday_directive_text = f"SHORT {symbol} @ ₹{last_closed_price} | SL: ₹{underlying_sl} | Target: ₹{underlying_target} (1:2 R:R) | Exit by 15:15 IST"
            
            if category == "INDEX_OPTIONS":
                risk_management = {
                    "contract_type": "WEEKLY_PUT",
                    "action": f"BUY {put_strike_name}",
                    "premium_stop_loss_pct": meta.get("max_sl_premium_pct", 45.0),
                    "premium_target_pct": meta.get("target_gain_pct", 180.0),
                    "capital_guidance": "Small capital allocation. Max 45% premium SL, 180%+ Target."
                }
            elif category == "STOCK_OPTIONS":
                risk_management = {
                    "contract_type": "MONTHLY_PUT_ATM",
                    "action": f"BUY {put_strike_name}",
                    "underlying_entry": last_closed_price,
                    "underlying_sl": underlying_sl,
                    "underlying_target": underlying_target,
                    "premium_stop_loss_pct": 25.0,
                    "rr_ratio": "1:2.0"
                }
            else: # INTRADAY_CASH
                risk_management = {
                    "segment": "EQUITY_CASH",
                    "action": f"SHORT {symbol} (CASH INTRADAY)",
                    "entry_price": last_closed_price,
                    "stop_loss": underlying_sl,
                    "target": underlying_target,
                    "rr_ratio": "1:2.0",
                    "anti_circuit": anti_circuit_status,
                    "square_off": "Mandatory square-off before 15:15 IST"
                }
        else:
            # Standby inside range
            underlying_sl = round(range_bot_val, 2)
            underlying_target = round(range_top_val, 2)
            risk_management = {
                "action": "HOLD CASH / NO ENTRY",
                "call_strike_watch": call_strike_name if not is_cash_segment else "-",
                "put_strike_watch": put_strike_name if not is_cash_segment else "-"
            }

        is_overextended_flag = ignition_state in ("OVEREXTENDED_RUNNER", "OVEREXTENDED_SHORT")
        if last_closed_price > range_top_val:
            move_pct_val = round(((last_closed_price - range_top_val) / range_top_val) * 100.0, 2)
        elif last_closed_price < range_bot_val:
            move_pct_val = round(((last_closed_price - range_bot_val) / range_bot_val) * 100.0, 2)
        else:
            move_pct_val = 0.0

        return {
            "symbol": symbol,
            "name": meta.get("name", symbol),
            "category": meta.get("category", "STOCK"),
            "importance": meta.get("importance", "HIGH"),
            "lot_size": meta.get("lot_size", 100),
            "last_price": round(last_closed_price, 2),
            "is_closed_candle": True,
            "pdc": round(pdc, 2),
            "pdh": round(pdh, 2),
            "pdl": round(pdl, 2),
            "vwap": round(last_vwap, 2),
            "ema50": round(last_ema50, 2),
            "ema50_dist_pct": ema50_dist_pct,
            "above_ema50": above_ema50,
            "pct_change_pdc": pct_change_pdc,
            "rvol": rvol,
            "range_top": round(range_top_val, 2),
            "range_bottom": round(range_bot_val, 2),
            "range_mid": round(range_mid_val, 2),
            "is_coiling": is_coiling,
            "is_overextended": is_overextended_flag,
            "move_from_breakout_pct": move_pct_val,
            "ignition_state": ignition_state,
            "tier_label": tier_label,
            "confluence_score": confluence_score,
            "points_breakdown": points_breakdown,
            "signal_direction": signal_direction,
            "reason": reason,
            
            # Explicit Option & Intraday Action Fields
            "call_strike": call_strike_name,
            "put_strike": put_strike_name,
            "option_action_badge": option_action_badge,
            "intraday_action_badge": intraday_action_badge,
            "option_directive": option_directive_text,
            "intraday_directive": intraday_directive_text,
            
            "recommended_contract": recommended_contract,
            "anti_circuit_status": anti_circuit_status,
            "risk_management": risk_management,
            "timestamp_utc": get_utc_iso()
        }
