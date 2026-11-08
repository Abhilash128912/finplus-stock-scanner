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
        
        # Confluence Scoring
        pts_range = 0
        pts_pdh_pdl = 0
        pts_vwap = 0
        pts_volume = 0
        signal_direction = 0
        ignition_state = "NEUTRAL"
        tier_label = "CONSOLIDATION"
        
        broke_top = last_closed_price > range_top_val
        broke_bot = last_closed_price < range_bot_val
        above_pdh = last_closed_price > pdh
        below_pdl = last_closed_price < pdl
        above_vwap = last_closed_price > last_vwap
        below_vwap = last_closed_price < last_vwap
        has_volume = rvol >= 1.25
        
        if is_coiling:
            ignition_state = "COILING_ALERT"
            tier_label = "PRE-IGNITION SQUEEZE"
            reason = "Spring coiled: 20 closed candles compressed within ±1 ATR band."
        elif broke_top:
            signal_direction = 1
            pts_range = 35
            if above_pdh:
                pts_pdh_pdl = 30
            if above_vwap:
                pts_vwap = 20
            if has_volume:
                pts_volume = 15
                
            total_pts = pts_range + pts_pdh_pdl + pts_vwap + pts_volume
            if total_pts >= 65:
                ignition_state = "BULLISH_IGNITION"
                tier_label = "TIER-1 HIGH CONVICTION"
                reason = f"Closed above Range Top ({range_top_val:.2f})" + (" & PDH" if above_pdh else " + VWAP")
            elif total_pts >= 50:
                ignition_state = "RANGE_BREAKOUT_LONG"
                tier_label = "TIER-2 EARLY SCALP"
                reason = f"Closed above Range Top ({range_top_val:.2f}) with VWAP alignment"
            else:
                ignition_state = "MILD_BREAKOUT_LONG"
                tier_label = "WATCHLIST ONLY"
                reason = f"Range broke without strong price/volume support"

        elif broke_bot:
            signal_direction = -1
            pts_range = 35
            if below_pdl:
                pts_pdh_pdl = 30
            if below_vwap:
                pts_vwap = 20
            if has_volume:
                pts_volume = 15
                
            total_pts = pts_range + pts_pdh_pdl + pts_vwap + pts_volume
            if total_pts >= 65:
                ignition_state = "BEARISH_IGNITION"
                tier_label = "TIER-1 HIGH CONVICTION"
                reason = f"Closed below Range Bottom ({range_bot_val:.2f})" + (" & PDL" if below_pdl else " - VWAP")
            elif total_pts >= 50:
                ignition_state = "RANGE_BREAKDOWN_SHORT"
                tier_label = "TIER-2 EARLY SCALP"
                reason = f"Closed below Range Bottom ({range_bot_val:.2f}) with VWAP alignment"
            else:
                ignition_state = "MILD_BREAKDOWN_SHORT"
                tier_label = "WATCHLIST ONLY"
                reason = f"Range breakdown without strong price/volume support"
        else:
            reason = "Inside Range / Normal Consolidation"

        points_breakdown = {
            "range_breakout_pts": pts_range,
            "pdh_pdl_pts": pts_pdh_pdl,
            "vwap_pts": pts_vwap,
            "volume_pts": pts_volume,
            "total_score": pts_range + pts_pdh_pdl + pts_vwap + pts_volume
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
            "pct_change_pdc": pct_change_pdc,
            "rvol": rvol,
            "range_top": round(range_top_val, 2),
            "range_bottom": round(range_bot_val, 2),
            "range_mid": round(range_mid_val, 2),
            "is_coiling": is_coiling,
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
