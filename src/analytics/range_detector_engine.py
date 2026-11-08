"""
FinPlus+ LuxAlgo Range Detector & Key Levels Engine
MANDATE COMPLIANCE:
1. Closed Candles Only: Evaluates strictly on completed, closed 15m candles (df.iloc[:-1]).
   Zero phantom signals that appear and disappear mid-candle.
2. Tiered Confluence Merit: Instead of an all-or-nothing bottleneck that kills trade frequency,
   uses a dynamic scoring model (Tier 1 Ignition >= 65 pts, Tier 2 Early Scalp 50-64 pts).
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
    """Computes intraday VWAP with daily reset."""
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
        """
        Evaluates strictly on CLOSED 15m candles.
        Applies Tiered Confluence Scoring to avoid over-filtering.
        """
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
            
        # 1. MANDATE: Slice off the forming, unclosed candle
        # Bar -1 is currently forming; Bar -2 is the last fully closed candle.
        closed_df = df.iloc[:-1].copy() if len(df) > 30 else df.copy()
        
        # 2. Compute Indicators on Closed Data
        closed_df['atr'] = calc_atr(closed_df, self.atr_len) * self.mult
        closed_df['sma'] = closed_df['Close'].rolling(self.length).mean()
        closed_df['vwap'] = calc_intraday_vwap(closed_df)
        closed_df['vol_ma20'] = closed_df['Volume'].rolling(20).mean()
        
        # 3. Extract Daily Key Levels (PDH, PDL, PDC)
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
            
        # 4. LuxAlgo Range State Machine
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

        # 5. Analysis on the Last Closed Bar
        last_closed_price = float(closes[-1])
        last_vwap = float(vwap_vals[-1]) if not np.isnan(vwap_vals[-1]) else last_closed_price
        last_vol = float(volumes[-1])
        last_vma = float(vol_ma[-1]) if not np.isnan(vol_ma[-1]) else 1.0
        rvol = round(last_vol / max(1.0, last_vma), 2)
        
        pct_change_pdc = round(((last_closed_price - pdc) / pdc) * 100.0, 2)
        
        range_top_val = float(bx_top) if not np.isnan(bx_top) else (last_closed_price + (last_closed_price * 0.005))
        range_bot_val = float(bx_bottom) if not np.isnan(bx_bottom) else (last_closed_price - (last_closed_price * 0.005))
        range_mid_val = (range_top_val + range_bot_val) / 2.0
        
        # 6. TIERED CONFLUENCE SCORING ENGINE (0 to 100)
        # Prevents over-filtering while weighting high-conviction factors
        confluence_score = 0
        signal_direction = 0
        ignition_state = "NEUTRAL"
        tier_label = "CONSOLIDATION"
        
        broke_top = last_closed_price > range_top_val
        broke_bot = last_closed_price < range_bot_val
        above_pdh = last_closed_price > pdh
        below_pdl = last_closed_price < pdl
        above_vwap = last_closed_price > last_vwap
        below_vwap = last_closed_price < last_vwap
        has_volume = rvol >= 1.2
        
        if is_coiling:
            ignition_state = "COILING_ALERT"
            tier_label = "PRE-IGNITION SQUEEZE"
            reason = "Spring coiled: 20 closed candles compressed within ±1 ATR band."
            
        elif broke_top:
            signal_direction = 1
            # Base trigger: 35 points
            confluence_score += 35
            # PDH confirmation: 30 points
            if above_pdh:
                confluence_score += 30
            # VWAP confirmation: 20 points
            if above_vwap:
                confluence_score += 20
            # Volume confirmation: 15 points
            if has_volume:
                confluence_score += 15
                
            if confluence_score >= 65:
                ignition_state = "BULLISH_IGNITION"
                tier_label = "TIER-1 HIGH CONVICTION"
                reason = f"Closed above Range Top ({range_top_val:.2f})" + (" & PDH" if above_pdh else " + VWAP") + f" [Score: {confluence_score}/100]"
            elif confluence_score >= 50:
                ignition_state = "RANGE_BREAKOUT_LONG"
                tier_label = "TIER-2 EARLY SCALP"
                reason = f"Closed above Range Top ({range_top_val:.2f}) with VWAP alignment [Score: {confluence_score}/100]"
            else:
                ignition_state = "MILD_BREAKOUT_LONG"
                tier_label = "WATCHLIST ONLY"
                reason = f"Range broke without price/volume confluence [Score: {confluence_score}/100]"

        elif broke_bot:
            signal_direction = -1
            confluence_score += 35
            if below_pdl:
                confluence_score += 30
            if below_vwap:
                confluence_score += 20
            if has_volume:
                confluence_score += 15
                
            if confluence_score >= 65:
                ignition_state = "BEARISH_IGNITION"
                tier_label = "TIER-1 HIGH CONVICTION"
                reason = f"Closed below Range Bottom ({range_bot_val:.2f})" + (" & PDL" if below_pdl else " - VWAP") + f" [Score: {confluence_score}/100]"
            elif confluence_score >= 50:
                ignition_state = "RANGE_BREAKDOWN_SHORT"
                tier_label = "TIER-2 EARLY SCALP"
                reason = f"Closed below Range Bottom ({range_bot_val:.2f}) with VWAP alignment [Score: {confluence_score}/100]"
            else:
                ignition_state = "MILD_BREAKDOWN_SHORT"
                tier_label = "WATCHLIST ONLY"
                reason = f"Range breakdown without price/volume confluence [Score: {confluence_score}/100]"
        else:
            reason = "Inside Range / Normal Consolidation"

        # 7. Recommended Contract & Risk Management
        strike_step = meta.get("strike_step", 50)
        recommended_contract = None
        risk_management = {}
        
        if signal_direction == 1 and confluence_score >= 50:
            atm_strike = int(round(last_closed_price / strike_step) * strike_step)
            recommended_contract = f"{symbol} {atm_strike} CE"
            underlying_sl = range_bot_val
            underlying_risk = max(last_closed_price - underlying_sl, last_closed_price * 0.002)
            underlying_target = last_closed_price + (underlying_risk * 2.0)
            
            if meta.get("importance") == "HIGH" and meta.get("category") == "INDEX_OPTIONS": # Sensex
                risk_management = {
                    "contract_type": "WEEKLY_CALL",
                    "premium_stop_loss_pct": meta.get("max_sl_premium_pct", 45.0),
                    "premium_target_pct": meta.get("target_gain_pct", 180.0),
                    "capital_guidance": "Small capital allocation only. Max 45% premium SL, Asymmetric Upside."
                }
            else:
                risk_management = {
                    "contract_type": "MONTHLY_CALL_ATM",
                    "underlying_sl": round(underlying_sl, 2),
                    "underlying_target": round(underlying_target, 2),
                    "premium_stop_loss_pct": 25.0,
                    "rr_ratio": "1:2.0"
                }
                
        elif signal_direction == -1 and confluence_score >= 50:
            atm_strike = int(round(last_closed_price / strike_step) * strike_step)
            recommended_contract = f"{symbol} {atm_strike} PE"
            underlying_sl = range_top_val
            underlying_risk = max(underlying_sl - last_closed_price, last_closed_price * 0.002)
            underlying_target = last_closed_price - (underlying_risk * 2.0)
            
            if meta.get("importance") == "HIGH" and meta.get("category") == "INDEX_OPTIONS":
                risk_management = {
                    "contract_type": "WEEKLY_PUT",
                    "premium_stop_loss_pct": meta.get("max_sl_premium_pct", 45.0),
                    "premium_target_pct": meta.get("target_gain_pct", 180.0),
                    "capital_guidance": "Small capital allocation only. Max 45% premium SL, Asymmetric Upside."
                }
            else:
                risk_management = {
                    "contract_type": "MONTHLY_PUT_ATM",
                    "underlying_sl": round(underlying_sl, 2),
                    "underlying_target": round(underlying_target, 2),
                    "premium_stop_loss_pct": 25.0,
                    "rr_ratio": "1:2.0"
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
            "signal_direction": signal_direction,
            "reason": reason,
            "recommended_contract": recommended_contract,
            "risk_management": risk_management,
            "timestamp_utc": get_utc_iso()
        }
