"""
FinPlus+ Multi-Setup Engine (Phase 5 & 6 / v2.7.0)
Implements:
1. Two-Stage Signals: WATCH vs TRIGGER with 0.15% buffer and MAX_CHASE_PCT (0.50%) lateness gate (Phase 5d).
2. Structural Stop Loss with 1.0% Cash / 30% Options wide SL detection (Phase 5e).
3. Multiple Setups (Phase 6b):
   - Setup 1: RANGE_BREAKOUT / RANGE_BREAKDOWN (Lux range detector)
   - Setup 2: PDH_BREAKOUT / PDL_BREAKDOWN (Previous Day High/Low break with RVOL > 1.5)
   - Setup 3: VWAP_RECLAIM / VWAP_BREAKDOWN (Price crossing back over VWAP after 10:30 IST)
   - Setup 4: ORB_15M_BREAKOUT / ORB_15M_BREAKDOWN (First 15m candle break after 09:30 IST)
   - Setup 5: MOMENTUM_RUNNER (3 consecutive higher-high 15m candles with expanding volume)
4. Score-based candidate ranking (Phase 6c) for panel menu suggestions & NTFY alerts.
"""

import math
from typing import Dict, Any, List, Optional
import datetime
import pandas as pd
import numpy as np

from src.utils.time_and_logger import setup_logger, get_utc_iso, get_ist_now

logger = setup_logger("MultiSetupEngine")

MAX_CHASE_PCT = 0.50 # Skip if price has already moved > 0.50% past the trigger boundary
TRIGGER_BUFFER_PCT = 0.0015 # +0.15% boundary confirmation buffer

class MultiSetupEngine:
    def __init__(self, max_chase_pct: float = MAX_CHASE_PCT):
        self.max_chase_pct = max_chase_pct
        self.last_watch_alert_time: Dict[str, float] = {} # Symbol -> timestamp

    def evaluate_setups(
        self,
        closed_df: pd.DataFrame,
        eval_res: Dict[str, Any],
        meta: Dict[str, Any],
        now_ist: Optional[datetime.datetime] = None
    ) -> List[Dict[str, Any]]:
        """
        Evaluates all 5 institutional setups on closed bars and instantaneous live quote.
        Returns a list of candidate dictionaries (WATCH or TRIGGER).
        """
        if closed_df.empty or len(closed_df) < 5:
            return []

        now_ist = now_ist or get_ist_now()
        symbol = meta.get("symbol", eval_res.get("symbol", "UNKNOWN"))
        category = meta.get("category", "INTRADAY_CASH")
        live_p = float(eval_res.get("live_ltp", eval_res.get("last_price", 0.0)))
        if live_p <= 0:
            return []

        rvol = float(eval_res.get("rvol", 1.0))
        pdh = float(eval_res.get("pdh", 0.0))
        pdl = float(eval_res.get("pdl", 0.0))
        vwap = float(eval_res.get("vwap", 0.0))
        range_top = float(eval_res.get("range_top", 0.0))
        range_bot = float(eval_res.get("range_bottom", 0.0))
        is_coiling = bool(eval_res.get("is_coiling", False))
        regime_conf = eval_res.get("regime_confluence", "APPROVED")
        feed_status = eval_res.get("feed_status", "NORMAL")

        candidates: List[Dict[str, Any]] = []

        # -------------------------------------------------------------
        # SETUP 1: RANGE_BREAKOUT / RANGE_BREAKDOWN (LuxAlgo)
        # -------------------------------------------------------------
        sig_dir = eval_res.get("signal_direction", 0)
        if range_top > 0:
            # Bullish Range Breakout
            dist_to_top = (range_top - live_p) / range_top
            if sig_dir == 1 or live_p >= (range_top * (1.0 + TRIGGER_BUFFER_PCT)):
                sl_val = range_bot if range_bot > 0 else round(live_p * 0.992, 2)
                candidates.append(self._build_candidate(
                    setup_name="RANGE_BREAKOUT",
                    action="BUY",
                    symbol=symbol,
                    sector=category,
                    boundary=range_top,
                    live_price=live_p,
                    stop_loss=sl_val,
                    rvol=rvol,
                    regime_conf=regime_conf,
                    feed_status=feed_status,
                    notes=f"Lux Range Top Breakout above ₹{range_top:.2f}"
                ))
            elif 0.001 <= dist_to_top <= 0.005 and rvol >= 1.0 and is_coiling:
                candidates.append(self._build_watch_candidate(
                    setup_name="RANGE_BREAKOUT",
                    action="BUY",
                    symbol=symbol,
                    sector=category,
                    boundary=range_top,
                    live_price=live_p,
                    rvol=rvol,
                    notes="Coiling within 0.1-0.5% below Range Top"
                ))

        if range_bot > 0:
            # Bearish Range Breakdown
            dist_to_bot = (live_p - range_bot) / range_bot
            if sig_dir == -1 or live_p <= (range_bot * (1.0 - TRIGGER_BUFFER_PCT)):
                sl_val = range_top if range_top > 0 else round(live_p * 1.008, 2)
                candidates.append(self._build_candidate(
                    setup_name="RANGE_BREAKDOWN",
                    action="SELL",
                    symbol=symbol,
                    sector=category,
                    boundary=range_bot,
                    live_price=live_p,
                    stop_loss=sl_val,
                    rvol=rvol,
                    regime_conf=regime_conf,
                    feed_status=feed_status,
                    notes=f"Lux Range Bottom Breakdown below ₹{range_bot:.2f}"
                ))
            elif 0.001 <= dist_to_bot <= 0.005 and rvol >= 1.0 and is_coiling:
                candidates.append(self._build_watch_candidate(
                    setup_name="RANGE_BREAKDOWN",
                    action="SELL",
                    symbol=symbol,
                    sector=category,
                    boundary=range_bot,
                    live_price=live_p,
                    rvol=rvol,
                    notes="Coiling within 0.1-0.5% above Range Bottom"
                ))

        # -------------------------------------------------------------
        # SETUP 2: PDH_BREAKOUT / PDL_BREAKDOWN (RVOL > 1.5)
        # -------------------------------------------------------------
        if pdh > 0:
            dist_to_pdh = (pdh - live_p) / pdh
            if live_p >= (pdh * (1.0 + TRIGGER_BUFFER_PCT)) and rvol >= 1.5:
                sl_val = round(pdh * 0.995, 2)
                candidates.append(self._build_candidate(
                    setup_name="PDH_BREAKOUT",
                    action="BUY",
                    symbol=symbol,
                    sector=category,
                    boundary=pdh,
                    live_price=live_p,
                    stop_loss=sl_val,
                    rvol=rvol,
                    regime_conf=regime_conf,
                    feed_status=feed_status,
                    notes=f"Previous Day High Breakout (₹{pdh:.2f}) on {rvol:.1f}x RVOL"
                ))
            elif 0.001 <= dist_to_pdh <= 0.005 and rvol >= 1.2:
                candidates.append(self._build_watch_candidate(
                    setup_name="PDH_BREAKOUT",
                    action="BUY",
                    symbol=symbol,
                    sector=category,
                    boundary=pdh,
                    live_price=live_p,
                    rvol=rvol,
                    notes=f"Approaching PDH ₹{pdh:.2f} with RVOL {rvol:.1f}x"
                ))

        if pdl > 0:
            dist_to_pdl = (live_p - pdl) / pdl
            if live_p <= (pdl * (1.0 - TRIGGER_BUFFER_PCT)) and rvol >= 1.5:
                sl_val = round(pdl * 1.005, 2)
                candidates.append(self._build_candidate(
                    setup_name="PDL_BREAKDOWN",
                    action="SELL",
                    symbol=symbol,
                    sector=category,
                    boundary=pdl,
                    live_price=live_p,
                    stop_loss=sl_val,
                    rvol=rvol,
                    regime_conf=regime_conf,
                    feed_status=feed_status,
                    notes=f"Previous Day Low Breakdown (₹{pdl:.2f}) on {rvol:.1f}x RVOL"
                ))
            elif 0.001 <= dist_to_pdl <= 0.005 and rvol >= 1.2:
                candidates.append(self._build_watch_candidate(
                    setup_name="PDL_BREAKDOWN",
                    action="SELL",
                    symbol=symbol,
                    sector=category,
                    boundary=pdl,
                    live_price=live_p,
                    rvol=rvol,
                    notes=f"Approaching PDL ₹{pdl:.2f} with RVOL {rvol:.1f}x"
                ))

        # -------------------------------------------------------------
        # SETUP 3: VWAP_RECLAIM / VWAP_BREAKDOWN (After 10:30 IST)
        # -------------------------------------------------------------
        is_after_1030 = (now_ist.hour > 10) or (now_ist.hour == 10 and now_ist.minute >= 30)
        if is_after_1030 and vwap > 0 and len(closed_df) >= 2:
            prev_close = float(closed_df['Close'].iloc[-2])
            # Bullish VWAP Reclaim: Prior candle closed below VWAP, now crossed above
            if prev_close < vwap and live_p >= (vwap * (1.0 + TRIGGER_BUFFER_PCT)):
                sl_val = round(vwap * 0.995, 2)
                candidates.append(self._build_candidate(
                    setup_name="VWAP_RECLAIM",
                    action="BUY",
                    symbol=symbol,
                    sector=category,
                    boundary=vwap,
                    live_price=live_p,
                    stop_loss=sl_val,
                    rvol=rvol,
                    regime_conf=regime_conf,
                    feed_status=feed_status,
                    notes=f"Bullish VWAP Reclaim across ₹{vwap:.2f} after 10:30 IST"
                ))
            # Bearish VWAP Breakdown: Prior candle closed above VWAP, now crossed below
            elif prev_close > vwap and live_p <= (vwap * (1.0 - TRIGGER_BUFFER_PCT)):
                sl_val = round(vwap * 1.005, 2)
                candidates.append(self._build_candidate(
                    setup_name="VWAP_BREAKDOWN",
                    action="SELL",
                    symbol=symbol,
                    sector=category,
                    boundary=vwap,
                    live_price=live_p,
                    stop_loss=sl_val,
                    rvol=rvol,
                    regime_conf=regime_conf,
                    feed_status=feed_status,
                    notes=f"Bearish VWAP Breakdown below ₹{vwap:.2f} after 10:30 IST"
                ))

        # -------------------------------------------------------------
        # SETUP 4: ORB_15M_BREAKOUT / ORB_15M_BREAKDOWN (After 09:30 IST)
        # -------------------------------------------------------------
        is_after_0930 = (now_ist.hour > 9) or (now_ist.hour == 9 and now_ist.minute >= 30)
        if is_after_0930 and len(closed_df) >= 2:
            try:
                today_date = now_ist.date()
                dates = pd.to_datetime(closed_df.index).date
                today_indices = np.where(dates == today_date)[0]
                if len(today_indices) >= 1:
                    first_bar = closed_df.iloc[today_indices[0]]
                    orb_h = float(first_bar['High'])
                    orb_l = float(first_bar['Low'])
                    if orb_h > orb_l and orb_l > 0:
                        # ORB Long Breakout
                        if live_p >= (orb_h * (1.0 + TRIGGER_BUFFER_PCT)):
                            sl_dist = (live_p - orb_l) / live_p
                            sl_val = orb_l if sl_dist <= 0.01 else round((orb_h + orb_l) / 2.0, 2)
                            candidates.append(self._build_candidate(
                                setup_name="ORB_15M_BREAKOUT",
                                action="BUY",
                                symbol=symbol,
                                sector=category,
                                boundary=orb_h,
                                live_price=live_p,
                                stop_loss=sl_val,
                                rvol=rvol,
                                regime_conf=regime_conf,
                                feed_status=feed_status,
                                notes=f"15m Opening Range Breakout above ₹{orb_h:.2f}"
                            ))
                        # ORB Short Breakdown
                        elif live_p <= (orb_l * (1.0 - TRIGGER_BUFFER_PCT)):
                            sl_dist = (orb_h - live_p) / live_p
                            sl_val = orb_h if sl_dist <= 0.01 else round((orb_h + orb_l) / 2.0, 2)
                            candidates.append(self._build_candidate(
                                setup_name="ORB_15M_BREAKDOWN",
                                action="SELL",
                                symbol=symbol,
                                sector=category,
                                boundary=orb_l,
                                live_price=live_p,
                                stop_loss=sl_val,
                                rvol=rvol,
                                regime_conf=regime_conf,
                                feed_status=feed_status,
                                notes=f"15m Opening Range Breakdown below ₹{orb_l:.2f}"
                            ))
            except Exception as orb_err:
                logger.debug(f"ORB 15m calculation note for {symbol}: {orb_err}")

        # -------------------------------------------------------------
        # SETUP 4b: ORB_30M_BREAKOUT / ORB_30M_BREAKDOWN (After 09:45 IST)
        # -------------------------------------------------------------
        is_after_0945 = (now_ist.hour > 9) or (now_ist.hour == 9 and now_ist.minute >= 45)
        if is_after_0945 and len(closed_df) >= 3:
            try:
                today_date = now_ist.date()
                dates = pd.to_datetime(closed_df.index).date
                today_indices = np.where(dates == today_date)[0]
                if len(today_indices) >= 2:
                    # First two 15m candles constitute the 30-minute opening range
                    first_two = closed_df.iloc[today_indices[:2]]
                    orb30_h = float(first_two['High'].max())
                    orb30_l = float(first_two['Low'].min())
                    if orb30_h > orb30_l and orb30_l > 0:
                        # ORB 30m Long Breakout
                        if live_p >= (orb30_h * (1.0 + TRIGGER_BUFFER_PCT)):
                            sl_dist = (live_p - orb30_l) / live_p
                            sl_val = orb30_l if sl_dist <= 0.01 else round((orb30_h + orb30_l) / 2.0, 2)
                            candidates.append(self._build_candidate(
                                setup_name="ORB_30M_BREAKOUT",
                                action="BUY",
                                symbol=symbol,
                                sector=category,
                                boundary=orb30_h,
                                live_price=live_p,
                                stop_loss=sl_val,
                                rvol=rvol,
                                regime_conf=regime_conf,
                                feed_status=feed_status,
                                notes=f"30m Opening Range Breakout above ₹{orb30_h:.2f}"
                            ))
                        # ORB 30m Short Breakdown
                        elif live_p <= (orb30_l * (1.0 - TRIGGER_BUFFER_PCT)):
                            sl_dist = (orb30_h - live_p) / live_p
                            sl_val = orb30_h if sl_dist <= 0.01 else round((orb30_h + orb30_l) / 2.0, 2)
                            candidates.append(self._build_candidate(
                                setup_name="ORB_30M_BREAKDOWN",
                                action="SELL",
                                symbol=symbol,
                                sector=category,
                                boundary=orb30_l,
                                live_price=live_p,
                                stop_loss=sl_val,
                                rvol=rvol,
                                regime_conf=regime_conf,
                                feed_status=feed_status,
                                notes=f"30m Opening Range Breakdown below ₹{orb30_l:.2f}"
                            ))
            except Exception as orb30_err:
                logger.debug(f"ORB 30m calculation note for {symbol}: {orb30_err}")

        # -------------------------------------------------------------
        # SETUP 5: MOMENTUM_RUNNER (3 Consecutive HH 15m Candles + Expanding Vol)
        # -------------------------------------------------------------
        if len(closed_df) >= 3:
            c1 = closed_df.iloc[-3]
            c2 = closed_df.iloc[-2]
            c3 = closed_df.iloc[-1]
            try:
                h1, h2, h3 = float(c1['High']), float(c2['High']), float(c3['High'])
                l1, l2, l3 = float(c1['Low']), float(c2['Low']), float(c3['Low'])
                v1, v2, v3 = float(c1['Volume']), float(c2['Volume']), float(c3['Volume'])
                c3_close = float(c3['Close'])
                c3_open = float(c3['Open'])

                # Long Momentum Runner: 3 Higher Highs + Expanding Volume
                if h3 > h2 > h1 and v3 > v2 > v1 and c3_close > c3_open:
                    if live_p >= (h3 * (1.0 + TRIGGER_BUFFER_PCT)):
                        sl_val = round(float(c1['Low']), 2)
                        candidates.append(self._build_candidate(
                            setup_name="MOMENTUM_RUNNER",
                            action="BUY",
                            symbol=symbol,
                            sector=category,
                            boundary=h3,
                            live_price=live_p,
                            stop_loss=sl_val,
                            rvol=rvol,
                            regime_conf=regime_conf,
                            feed_status=feed_status,
                            notes=f"3-Candle Higher-High Momentum Runner with Expanding Volume (Vol {v3:,.0f})"
                        ))
                # Short Momentum Breakdown: 3 Lower Lows + Expanding Volume
                elif l3 < l2 < l1 and v3 > v2 > v1 and c3_close < c3_open:
                    if live_p <= (l3 * (1.0 - TRIGGER_BUFFER_PCT)):
                        sl_val = round(float(c1['High']), 2)
                        candidates.append(self._build_candidate(
                            setup_name="MOMENTUM_RUNNER_SHORT",
                            action="SELL",
                            symbol=symbol,
                            sector=category,
                            boundary=l3,
                            live_price=live_p,
                            stop_loss=sl_val,
                            rvol=rvol,
                            regime_conf=regime_conf,
                            feed_status=feed_status,
                            notes=f"3-Candle Lower-Low Momentum Breakdown with Expanding Volume (Vol {v3:,.0f})"
                        ))
            except Exception:
                pass

        return candidates

    def _build_candidate(
        self,
        setup_name: str,
        action: str,
        symbol: str,
        sector: str,
        boundary: float,
        live_price: float,
        stop_loss: float,
        rvol: float,
        regime_conf: str,
        feed_status: str,
        notes: str
    ) -> Dict[str, Any]:
        """Constructs a standard TRIGGER candidate dictionary with lateness and wide SL checks."""
        # Lateness calculation (Phase 5d)
        if action == "BUY":
            lateness_pct = round(((live_price - boundary) / boundary) * 100.0, 2)
        else:
            lateness_pct = round(((boundary - live_price) / boundary) * 100.0, 2)

        is_late = (lateness_pct > self.max_chase_pct)
        status = "LATE (skipped)" if is_late else "TRIGGER"

        # Structural Stop Loss & Wide SL check (Phase 5e)
        sl_dist_pct = round(abs(live_price - stop_loss) / live_price * 100.0, 2) if live_price > 0 else 0.0
        is_option = sector in ("STOCK_OPTIONS", "INDEX_OPTIONS")
        is_wide_sl = (sl_dist_pct > 30.0) if is_option else (sl_dist_pct > 1.0)

        setup_key = f"{setup_name}_{action.upper()}_{sector}"

        return {
            "setup_name": setup_name,
            "setup_key": setup_key,
            "symbol": symbol,
            "sector": sector,
            "action": action.upper(),
            "status": status,
            "stage_type": "TRIGGER",
            "boundary": round(boundary, 2),
            "entry_price": round(live_price, 2),
            "stop_loss": round(stop_loss, 2),
            "sl_dist_pct": sl_dist_pct,
            "is_wide_sl": is_wide_sl,
            "lateness_pct": lateness_pct,
            "is_late": is_late,
            "rvol": round(rvol, 2),
            "regime_confluence": regime_conf,
            "feed_status": feed_status,
            "notes": notes,
            "timestamp_utc": get_utc_iso()
        }

    def _build_watch_candidate(
        self,
        setup_name: str,
        action: str,
        symbol: str,
        sector: str,
        boundary: float,
        live_price: float,
        rvol: float,
        notes: str
    ) -> Dict[str, Any]:
        """Constructs a WATCH candidate dictionary (Phase 5d)."""
        dist_pct = round(abs(live_price - boundary) / boundary * 100.0, 2) if boundary > 0 else 0.0
        return {
            "setup_name": setup_name,
            "setup_key": f"{setup_name}_{action.upper()}_{sector}",
            "symbol": symbol,
            "sector": sector,
            "action": action.upper(),
            "status": "WATCH",
            "stage_type": "WATCH",
            "boundary": round(boundary, 2),
            "entry_price": round(live_price, 2),
            "stop_loss": 0.0,
            "sl_dist_pct": 0.0,
            "is_wide_sl": False,
            "lateness_pct": 0.0,
            "is_late": False,
            "rvol": round(rvol, 2),
            "regime_confluence": "APPROVED",
            "feed_status": "NORMAL",
            "notes": f"WATCH: {notes} (Dist: {dist_pct}%)",
            "timestamp_utc": get_utc_iso()
        }

    def score_candidate(self, candidate: Dict[str, Any], grade: str = "B") -> float:
        """
        Computes composite ranking score (Phase 6c):
        Setup Grade (A=30, B=15, C=0) + RVOL (max 30) + Regime (max 25) + Lateness (max 15) + SL (max 15)
        """
        score = 0.0
        # 1. Setup Grade
        if grade == "A":
            score += 30.0
        elif grade == "B":
            score += 15.0

        # 2. RVOL (capped at 30 pts)
        rvol = candidate.get("rvol", 1.0)
        score += min(rvol * 10.0, 30.0)

        # 3. Macro Regime Alignment
        conf = candidate.get("regime_confluence", "")
        if "APPROVED" in conf:
            score += 25.0
        elif "VETOED" in conf:
            score -= 50.0
        else:
            score += 10.0

        # 4. Lateness (lower is better, max 15 pts)
        lat = candidate.get("lateness_pct", 0.0)
        score += max(0.0, 15.0 - (lat * 30.0))

        # 5. Stop Loss Tightness (max 15 pts)
        sl_pct = candidate.get("sl_dist_pct", 1.0)
        if sl_pct <= 0.6:
            score += 15.0
        elif sl_pct <= 1.0:
            score += 10.0
        elif sl_pct <= 1.5:
            score += 5.0

        return round(score, 1)

