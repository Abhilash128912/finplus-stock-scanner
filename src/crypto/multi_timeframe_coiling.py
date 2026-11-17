"""
FinPlus+ Crypto Delta - Multi-Timeframe Coiling & Squeeze Analytics
LuxAlgo-inspired 5m & 15m volatility squeeze, volume surge, and dynamic ATR targeting.
Implements authentic Pine Script LuxAlgo Range Detector (20, 1, 500/100).
"""

from typing import Dict, Any, List, Optional, Tuple
import math
import numpy as np
from src.utils.time_and_logger import setup_logger

logger = setup_logger("MultiTimeframeCoiling")

def calc_rma(series: List[float], length: int) -> List[float]:
    """
    Exact Pine Script ta.rma parity:
    First value = SMA of the first `length` items.
    Subsequent values = (prev * (length - 1) + val) / length.
    """
    if len(series) < length:
        return [0.0] * len(series)
    rma = [0.0] * len(series)
    first_sma = sum(series[:length]) / float(length)
    rma[length - 1] = first_sma
    for i in range(length, len(series)):
        rma[i] = (rma[i - 1] * (length - 1) + series[i]) / float(length)
    return rma


class MultiTimeframeCoilingEngine:
    def __init__(self, range_length: int = 20, mult: float = 1.0, atr_length: int = 100):
        self.range_length = range_length
        self.mult = mult
        self.atr_length = atr_length

    @staticmethod
    def calculate_ema(prices: List[float], period: int) -> List[float]:
        """Calculates Exponential Moving Average across a series."""
        if not prices or len(prices) < period:
            return []
        ema = [sum(prices[:period]) / period]
        multiplier = 2.0 / (period + 1.0)
        for p in prices[period:]:
            ema.append((p - ema[-1]) * multiplier + ema[-1])
        return ema

    @staticmethod
    def calculate_atr(candles: List[Dict[str, Any]], period: int = 14) -> float:
        """Calculates Average True Range over the specified candle period."""
        if len(candles) < period + 1:
            return 0.0
        tr_list = []
        for i in range(1, len(candles)):
            h = float(candles[i].get("high", 0.0))
            l = float(candles[i].get("low", 0.0))
            prev_c = float(candles[i-1].get("close", 0.0))
            tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
            tr_list.append(tr)
        
        if len(tr_list) < period:
            return 0.0
        return sum(tr_list[-period:]) / period

    def detect_luxalgo_range(
        self,
        candles_15m: List[Dict[str, Any]]
    ) -> Tuple[float, float, float, bool]:
        """
        Authentic Pine Script LuxAlgo Range Detector (length=20, mult=1.0).
        Identifies tight consolidation boxes and maintains fixed levels across candles until broken.
        Returns: (box_top, box_bottom, box_mid, is_coiling)
        """
        n_bars = len(candles_15m)
        closes = [float(c.get("close", 0.0)) for c in candles_15m]
        highs = [float(c.get("high", 0.0)) for c in candles_15m]
        lows = [float(c.get("low", 0.0)) for c in candles_15m]

        if n_bars < self.range_length:
            top = max(highs) if highs else 0.0
            bot = min(lows) if lows else 0.0
            return top, bot, (top + bot) / 2.0, False

        # 1. Compute True Range series
        tr = [highs[0] - lows[0]]
        for i in range(1, n_bars):
            tr.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))

        # 2. Compute RMA ATR series
        eff_atr_len = min(self.atr_length, max(14, n_bars // 2))
        atr_series = calc_rma(tr, eff_atr_len)

        # 3. Compute 20-period SMA
        sma_series = []
        for i in range(n_bars):
            if i < self.range_length - 1:
                sma_series.append(0.0)
            else:
                sma_series.append(sum(closes[i - self.range_length + 1: i + 1]) / float(self.range_length))

        # 4. Pine Script Box Detection State Machine
        bx_right = -1
        bx_top = float("nan")
        bx_bottom = float("nan")
        prev_count = 1
        is_coiling = False

        for n in range(self.range_length - 1, n_bars):
            curr_ma = sma_series[n]
            curr_atr = atr_series[n] * self.mult
            if curr_ma == 0.0 or curr_atr == 0.0:
                continue

            count = 0
            for i in range(self.range_length):
                if abs(closes[n - i] - curr_ma) > curr_atr:
                    count += 1

            if count == 0 and prev_count != 0:
                bx_right = n
                bx_top = curr_ma + curr_atr
                bx_bottom = curr_ma - curr_atr
            elif count == 0:
                bx_right = n

            prev_count = count
            if n == n_bars - 1:
                is_coiling = (count == 0)

        # Fallback if no clean box formed yet: use 20-bar window
        if math.isnan(bx_top) or math.isnan(bx_bottom):
            bx_top = max(highs[-self.range_length:])
            bx_bottom = min(lows[-self.range_length:])

        bx_mid = (bx_top + bx_bottom) / 2.0
        return bx_top, bx_bottom, bx_mid, is_coiling

    def analyze_symbol(
        self,
        symbol: str,
        candles_15m: List[Dict[str, Any]],
        candles_5m: Optional[List[Dict[str, Any]]] = None,
        ltp_override: Optional[float] = None
    ) -> Dict[str, Any]:
        """
        Runs complete 15m / 5m multi-timeframe squeeze analysis for an instrument.
        """
        if not candles_15m or len(candles_15m) < 20:
            return {
                "symbol": symbol,
                "status": "INSUFFICIENT_DATA",
                "confluence_score": 0,
                "directive": "HOLD_CASH",
                "reason": "Insufficient 15m candle history"
            }

        closes_15m = [float(c.get("close", 0.0)) for c in candles_15m]
        volumes_15m = [float(c.get("volume", 0.0)) for c in candles_15m]

        ltp = float(ltp_override) if ltp_override and ltp_override > 0 else closes_15m[-1]

        # 1. Moving Averages (20 EMA & 50 EMA)
        ema20_series = self.calculate_ema(closes_15m, 20)
        ema50_series = self.calculate_ema(closes_15m, 50) if len(closes_15m) >= 50 else []
        ema20 = ema20_series[-1] if ema20_series else ltp
        ema50 = ema50_series[-1] if ema50_series else ema20

        # 2. Authentic LuxAlgo Range Detection
        box_top, box_bottom, box_mid, is_coiling = self.detect_luxalgo_range(candles_15m)
        box_width_pct = ((box_top - box_bottom) / box_mid * 100.0) if box_mid > 0 else 0.0

        # 3. Volume Surge Ratio
        avg_vol_20 = sum(volumes_15m[-21:-1]) / 20.0 if len(volumes_15m) >= 21 else (volumes_15m[-1] if volumes_15m else 1.0)
        curr_vol = volumes_15m[-1] if volumes_15m else 0.0
        vol_ratio = round(curr_vol / avg_vol_20, 2) if avg_vol_20 > 0 else 1.0

        # 4. ATR for dynamic SL / Targets
        atr = self.calculate_atr(candles_15m, 14)
        if atr <= 0:
            atr = ltp * 0.01  # 1% fallback

        # 5. Multi-timeframe 5m alignment
        five_min_aligned = False
        five_min_bias = "NEUTRAL"
        if candles_5m and len(candles_5m) >= 20:
            closes_5m = [float(c.get("close", 0.0)) for c in candles_5m]
            ema20_5m = self.calculate_ema(closes_5m, 20)
            if ema20_5m:
                if closes_5m[-1] > ema20_5m[-1]:
                    five_min_bias = "BULLISH"
                else:
                    five_min_bias = "BEARISH"

        # 6. Confluence Scoring
        # A. Compression Points (0-35)
        compression_pts = 0
        if box_width_pct <= 1.5:
            compression_pts = 35
        elif box_width_pct <= 2.5:
            compression_pts = 25
        elif box_width_pct <= 3.5:
            compression_pts = 15
        elif box_width_pct <= 5.0:
            compression_pts = 5

        # B. Trend Alignment (0-30)
        # Check against both moving averages and breakdown/breakout relative to LuxAlgo Box
        trend_pts = 0
        trend_bias = "NEUTRAL"

        is_breakdown = ltp < box_bottom
        is_breakout = ltp > box_top

        if (ema20 > ema50 and ltp >= ema20) or (is_breakout and ltp > ema50):
            trend_pts = 30
            trend_bias = "BULLISH"
        elif (ema20 < ema50 and ltp <= ema20) or (is_breakdown and ltp < ema50):
            trend_pts = 30
            trend_bias = "BEARISH"
        elif abs(ltp - ema20) / ltp < 0.005:
            trend_pts = 15
            trend_bias = "COILING_AT_EMA"

        # C. Volume Surge (0-20)
        vol_pts = 0
        if vol_ratio >= 2.0:
            vol_pts = 20
        elif vol_ratio >= 1.5:
            vol_pts = 15
        elif vol_ratio >= 1.0:
            vol_pts = 10
        elif is_breakdown or is_breakout:
            vol_pts = 10  # Baseline momentum on confirmed range breach

        # D. 5m MTF Confirmation (0-15)
        mtf_pts = 0
        if five_min_bias == trend_bias and trend_bias in ["BULLISH", "BEARISH"]:
            mtf_pts = 15
            five_min_aligned = True
        elif five_min_bias == "NEUTRAL":
            mtf_pts = 8

        total_confluence = compression_pts + trend_pts + vol_pts + mtf_pts

        # 7. Directive & Order Triggers (Pure Trailing Stop-Loss — No Target Cap)
        directive = "HOLD_CASH"
        action = None
        stop_loss = None
        reason = ""

        # Breakout Above Box Top
        if is_breakout and (trend_bias == "BULLISH" or ltp > ema50):
            if total_confluence >= 80:
                directive = "LONG_BREAKOUT"
                action = "BUY_LONG"
                # Stop Loss: Robust structural invalidation (box_bottom), 1.8x ATR, with 1.5% min floor & 3.5% max cap
                structural_sl = box_bottom if box_bottom < ltp else (ltp - 1.8 * atr)
                atr_sl = ltp - (1.8 * atr)
                min_pct_sl = ltp * 0.985 # 1.5% minimum stop distance
                candidate_sl = min(structural_sl, atr_sl, min_pct_sl)
                max_allowed_sl = ltp * 0.965 # 3.5% maximum risk ceiling
                stop_loss = round(max(candidate_sl, max_allowed_sl), 4)
                sl_dist_pct = ((ltp - stop_loss) / ltp) * 100.0
                reason = f"Confirmed 15m Bullish Breakout above {box_top:,.2f} above 50 EMA ({ema50:,.2f}) with {vol_ratio}x volume (SL: {sl_dist_pct:.2f}% | Trailing)"
            else:
                directive = "WATCH_BREAKOUT"
                reason = f"Breaching box top {box_top:,.2f} above 50 EMA ({ema50:,.2f}) but awaiting volume confluence ({total_confluence}/100)"

        # Breakdown Below Box Bottom
        elif is_breakdown and (trend_bias == "BEARISH" or ltp < ema50):
            if total_confluence >= 80:
                directive = "SHORT_BREAKDOWN"
                action = "SELL_SHORT"
                # Stop Loss: Robust structural invalidation (box_top), 1.8x ATR, with 1.5% min floor & 3.5% max cap
                structural_sl = box_top if box_top > ltp else (ltp + 1.8 * atr)
                atr_sl = ltp + (1.8 * atr)
                min_pct_sl = ltp * 1.015 # 1.5% minimum stop distance
                candidate_sl = max(structural_sl, atr_sl, min_pct_sl)
                max_allowed_sl = ltp * 1.035 # 3.5% maximum risk ceiling
                stop_loss = round(min(candidate_sl, max_allowed_sl), 4)
                sl_dist_pct = ((stop_loss - ltp) / ltp) * 100.0
                reason = f"Confirmed 15m Bearish Breakdown below {box_bottom:,.2f} under 50 EMA ({ema50:,.2f}) with {vol_ratio}x volume (SL: {sl_dist_pct:.2f}% | Trailing)"
            else:
                directive = "WATCH_BREAKDOWN"
                reason = f"Breaching box bottom {box_bottom:,.2f} under 50 EMA ({ema50:,.2f}) but awaiting volume confluence ({total_confluence}/100)"

        # Coiling Inside Tight Box
        elif is_coiling or box_width_pct <= 2.0:
            directive = "COILING_COMPRESSION"
            reason = f"Coiling inside {box_width_pct:.2f}% volatility squeeze ({box_bottom:,.2f} - {box_top:,.2f}). Primed for ignition."

        else:
            directive = "STANDBY"
            reason = f"Range-bound consolidation ({box_bottom:,.2f} - {box_top:,.2f}). Distance to breakdown: {abs(ltp - box_bottom)/ltp*100:.2f}%"

        return {
            "symbol": symbol,
            "ltp": ltp,
            "box_top": round(box_top, 4),
            "box_bottom": round(box_bottom, 4),
            "box_mid": round(box_mid, 4),
            "box_width_pct": round(box_width_pct, 2),
            "vol_ratio": vol_ratio,
            "ema20": round(ema20, 4),
            "ema50": round(ema50, 4),
            "atr": round(atr, 4),
            "trend_bias": trend_bias,
            "five_min_bias": five_min_bias,
            "five_min_aligned": five_min_aligned,
            "confluence_score": total_confluence,
            "score_breakdown": {
                "compression_pts": compression_pts,
                "trend_pts": trend_pts,
                "volume_pts": vol_pts,
                "mtf_pts": mtf_pts
            },
            "directive": directive,
            "action": action,
            "entry_price": ltp if action else None,
            "stop_loss": stop_loss,
            "exit_strategy": "TRAILING_STOP_LOSS",
            "reason": reason
        }
