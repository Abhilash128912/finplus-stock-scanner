"""
FinPlus+ 4-Tier Multi-Horizon Institutional Intelligence Engine
App #2

Segments:
1. INTRADAY: High relative strength, 15m/5m VCP launchpad, L2 orderflow imbalance, VWAP + 50 EMA alignment.
2. SWING: 3-15 day holding, Stage-2 breakout (Close > 50 DMA > 200 DMA), weekly VCP contraction, high momentum.
3. LONG-TERM: Fundamental durability, Valuation score > 50, multi-year base breakout, steady compounding.
4. PENNY / HIGH-BETA RUNNER: Price < ₹100, volume explosion (RVOL > 2.5x), tight consolidation breakout, high reward.
"""

from typing import Dict, Any, List, Optional
import numpy as np
import pandas as pd
from src.utils.time_and_logger import setup_logger, get_utc_iso

logger = setup_logger("HorizonEngine")

class MultiHorizonScorer:
    """
    Computes dedicated 0-100 institutional scores across 4 distinct horizons:
    - Intraday (VCP + Orderflow + VWAP)
    - Swing (Stage-2 Mark Minervini + 50/200 DMA)
    - Long-Term (Durability + Valuation + Compounder Base)
    - Penny Runner (< ₹100 + Squeeze + Surge)
    """

    @staticmethod
    def score_intraday_vcp_orderflow(
        eval_res: Dict[str, Any],
        df: Optional[pd.DataFrame] = None,
        orderflow_data: Optional[Dict[str, Any]] = None,
        nifty_change_pct: float = 0.0
    ) -> Dict[str, Any]:
        """
        Institutional 6-Pillar Pre-Breakout & Tight Base (VCP) Intelligence Engine:
        1. Tight base near resistance (Price within 0.1% - 0.7% of Base High / HOD, base width < 1.2%)
        2. Volatility Contraction Pattern (VCP: candle spreads compressing, ATR pinching)
        3. Volume dry-up in base (Volume falls below 20-period MA, sellers exhausted)
        4. Relative strength vs Nifty (Holding base near highs while Nifty chops/dips)
        5. Trend alignment (Price > VWAP > 50 EMA)
        6. Stage-2 High-Tight Continuations (Does NOT bar runners like NETWEB; qualifies them if they form a fresh tight base)
        """
        pts = 0
        reasons = []
        
        last_price = float(eval_res.get("live_ltp") or eval_res.get("last_price", 0.0))
        pct_change = float(eval_res.get("pct_change_pdc", 0.0))
        vwap = float(eval_res.get("vwap", last_price))
        ema50 = float(eval_res.get("ema50", last_price))
        rvol = float(eval_res.get("rvol", 1.0))
        
        # 1. Base Geometry & Resistance Detection from Candle Data
        base_high = float(eval_res.get("range_top", last_price))
        base_low = float(eval_res.get("range_bottom", last_price * 0.99))
        base_width_pct = 1.0
        is_vcp_contracting = False
        is_volume_dry = False
        
        if df is not None and not df.empty and len(df) >= 3:
            try:
                # Analyze recent 3 to 6 intraday bars forming the micro-base
                recent_n = min(6, len(df))
                recent_bars = df.iloc[-recent_n:]
                b_high = float(np.max(recent_bars['High'].values))
                b_low = float(np.min(recent_bars['Low'].values))
                if b_high > 0:
                    base_high = b_high
                    base_low = b_low
                    base_width_pct = round(((base_high - base_low) / base_high) * 100.0, 2)
                
                # Check VCP spread compression across base
                spreads = ((recent_bars['High'].values - recent_bars['Low'].values) / recent_bars['Close'].values * 100.0)
                if len(spreads) >= 3:
                    half = len(spreads) // 2
                    s_earlier = float(np.mean(spreads[:half]))
                    s_recent = float(np.mean(spreads[half:]))
                    is_vcp_contracting = (s_recent <= s_earlier) or (base_width_pct <= 0.8)
                    
                # Check Volume Dry-Up in Base
                v_ma_series = df['Volume'].rolling(min(20, len(df))).mean()
                vol_ma = float(v_ma_series.values[-1])
                base_avg_vol = float(np.mean(recent_bars['Volume'].values))
                if vol_ma > 0:
                    vol_dry_ratio = round(base_avg_vol / vol_ma, 2)
                    is_volume_dry = (vol_dry_ratio <= 0.85)
            except Exception:
                pass

        is_short = int(eval_res.get("signal_direction", 0)) == -1 or "SHORT" in str(eval_res.get("intraday_action_badge", ""))

        if not is_short:
            # Distance to Breakout Resistance Pivot (Bullish)
            dist_to_pivot_pct = round(((base_high - last_price) / max(1.0, base_high)) * 100.0, 2)
            
            # Pillar 1: Tight Base Proximity to Resistance (30 pts)
            if 0.0 <= dist_to_pivot_pct <= 0.7 and base_width_pct <= 1.2:
                pts += 30
                reasons.append(f"Launchpad Squeeze (Coiled {dist_to_pivot_pct}% below ₹{base_high:.2f}, Base: {base_width_pct}%)")
            elif dist_to_pivot_pct < 0.0 and abs(dist_to_pivot_pct) <= 0.6:
                pts += 25
                reasons.append(f"Fresh Breakout (+{abs(dist_to_pivot_pct)}% through pivot ₹{base_high:.2f})")
            elif base_width_pct <= 0.8:
                pts += 20
                reasons.append(f"Ultra-Tight Base ({base_width_pct}% spread)")

            # Pillar 2: Volatility Contraction Pattern (VCP) (20 pts)
            if is_vcp_contracting and base_width_pct <= 1.0:
                pts += 20
                reasons.append("VCP Volatility Contraction (Candles compressing near resistance)")
            elif is_vcp_contracting or base_width_pct <= 1.2:
                pts += 12
                reasons.append("Moderate Volatility Compression")

            # Pillar 3: Volume Dry-Up (15 pts)
            if is_volume_dry:
                pts += 15
                reasons.append("Volume Dry-up in Base (Sellers exhausted)")
            elif rvol >= 1.2 and abs(dist_to_pivot_pct) <= 0.5:
                pts += 15
                reasons.append("Breakout Volume Expansion")
            elif rvol <= 0.8:
                pts += 10
                reasons.append("Low supply drag")

            # Pillar 4: Relative Strength vs Nifty (15 pts)
            rs_spread = round(pct_change - nifty_change_pct, 2)
            if rs_spread >= 1.5:
                pts += 15
                reasons.append(f"Superior Relative Strength (+{rs_spread}% vs Nifty)")
            elif rs_spread > 0.0:
                pts += 10
                reasons.append(f"Positive RS (+{rs_spread}% vs Nifty)")

            # Pillar 5: Trend & VWAP Support (10 pts)
            if last_price >= vwap and last_price >= ema50:
                pts += 10
                reasons.append("Price > VWAP & 50 EMA Support")
            elif last_price >= vwap:
                pts += 6

            # Pillar 6: Orderflow Imbalance (10 pts)
            buy_pct = 50.0
            if orderflow_data:
                buy_pct = float(orderflow_data.get("buy_percentage", 50.0))
                if buy_pct >= 65.0:
                    pts += 10
                    reasons.append(f"Orderflow: {buy_pct:.0f}% Institutional Bids")
                elif buy_pct >= 55.0:
                    pts += 5
            elif pts >= 60:
                pts += 7

            # Classification: Distinguish Launchpad, High-Tight Continuation (NETWEB), and Mid-Air Extension
            is_coiling_launchpad = (0.0 <= dist_to_pivot_pct <= 0.7 and base_width_pct <= 1.2 and last_price >= vwap)
            is_fresh_breakout = (dist_to_pivot_pct < 0.0 and abs(dist_to_pivot_pct) <= 0.6 and last_price >= vwap)
            is_continuation_runner = (pct_change >= 2.5 and is_coiling_launchpad)
            
            # Calculate institutional entry triggers & tight risk
            trigger_price = round(base_high + (base_high * 0.0005), 2)  # Tick above resistance
            tight_sl = round(base_low - (base_low * 0.0005), 2)         # Tick below base floor
            risk_per_share = max(last_price * 0.004, trigger_price - tight_sl)
            target_price = round(last_price + (risk_per_share * 2.2), 2) # 1:2.2 R:R

            state_label = "CONSOLIDATING"
            badge_label = "STANDBY"
            if is_continuation_runner:
                state_label = "STAGE2_CONTINUATION_LAUNCHPAD"
                badge_label = "⚡ HIGH-TIGHT BASE (STAGE-2 CONTINUATION)"
            elif is_coiling_launchpad:
                state_label = "PRE_IGNITION_LONG"
                badge_label = "⚡ PRE-IGNITION LAUNCHPAD (EARLY ENTRY)"
            elif is_fresh_breakout:
                state_label = "FRESH_BREAKOUT"
                badge_label = "🚀 FRESH BREAKOUT (TRIGGER CONFIRMED)"
            elif dist_to_pivot_pct < -1.5 and base_width_pct > 1.5:
                state_label = "AWAITING_BASE_FORMATION"
                badge_label = "WAIT FOR BASE / VWAP PULLBACK"
                pts = min(pts, 48)  # Don't buy mid-air vertical spikes without a base
        else:
            # Bearish / Short Breakdown Logic
            # Distance to Breakdown Floor (Support)
            dist_to_pivot_pct = round(((last_price - base_low) / max(1.0, base_low)) * 100.0, 2)
            
            # Pillar 1: Tight Base Proximity to Breakdown Support (30 pts)
            if 0.0 <= dist_to_pivot_pct <= 0.7 and base_width_pct <= 1.2:
                pts += 30
                reasons.append(f"Breakdown Squeeze (Coiled {dist_to_pivot_pct}% above floor ₹{base_low:.2f}, Base: {base_width_pct}%)")
            elif dist_to_pivot_pct < 0.0 and abs(dist_to_pivot_pct) <= 0.6:
                pts += 25
                reasons.append(f"Fresh Breakdown (-{abs(dist_to_pivot_pct)}% below floor ₹{base_low:.2f})")
            elif base_width_pct <= 0.8:
                pts += 20
                reasons.append(f"Ultra-Tight Breakdown Base ({base_width_pct}% spread)")

            # Pillar 2: Volatility Contraction Pattern (VCP) (20 pts)
            if is_vcp_contracting and base_width_pct <= 1.0:
                pts += 20
                reasons.append("VCP Volatility Contraction (Candles compressing near breakdown floor)")
            elif is_vcp_contracting or base_width_pct <= 1.2:
                pts += 12
                reasons.append("Moderate Volatility Compression")

            # Pillar 3: Volume Dry-Up or Breakdown Expansion (15 pts)
            if is_volume_dry:
                pts += 15
                reasons.append("Volume Dry-up in Distribution Base (Buyers exhausted)")
            elif rvol >= 1.2 and abs(dist_to_pivot_pct) <= 0.5:
                pts += 15
                reasons.append("Breakdown Volume Expansion")
            elif rvol <= 0.8:
                pts += 10
                reasons.append("Low buyer support drag")

            # Pillar 4: Relative Weakness vs Nifty (15 pts)
            rs_spread = round(pct_change - nifty_change_pct, 2)
            if rs_spread <= -1.5:
                pts += 15
                reasons.append(f"Superior Relative Weakness ({rs_spread}% vs Nifty)")
            elif rs_spread < 0.0:
                pts += 10
                reasons.append(f"Underperforming Nifty ({rs_spread}% vs Nifty)")

            # Pillar 5: Trend & VWAP Resistance (10 pts)
            if last_price <= vwap and last_price <= ema50:
                pts += 10
                reasons.append("Price < VWAP & 50 EMA Resistance")
            elif last_price <= vwap:
                pts += 6

            # Pillar 6: Orderflow Imbalance (10 pts)
            buy_pct = 50.0
            if orderflow_data:
                buy_pct = float(orderflow_data.get("buy_percentage", 50.0))
                sell_pct = 100.0 - buy_pct
                if sell_pct >= 65.0:
                    pts += 10
                    reasons.append(f"Orderflow: {sell_pct:.0f}% Institutional Asks (Selling)")
                elif sell_pct >= 55.0:
                    pts += 5
            elif pts >= 60:
                pts += 7

            is_coiling_launchpad = (0.0 <= dist_to_pivot_pct <= 0.7 and base_width_pct <= 1.2 and last_price <= vwap)
            is_fresh_breakout = (dist_to_pivot_pct < 0.0 and abs(dist_to_pivot_pct) <= 0.6 and last_price <= vwap)
            is_continuation_runner = (pct_change <= -2.5 and is_coiling_launchpad)

            # Downside trigger & stop loss
            trigger_price = round(base_low - (base_low * 0.0005), 2)  # Tick below support floor
            tight_sl = round(base_high + (base_high * 0.0005), 2)     # Tick above base ceiling
            risk_per_share = max(last_price * 0.004, tight_sl - trigger_price)
            target_price = round(last_price - (risk_per_share * 2.2), 2) # 1:2.2 R:R Downside

            state_label = "CONSOLIDATING"
            badge_label = "STANDBY"
            if is_continuation_runner:
                state_label = "STAGE2_DISTRIBUTION_SHORT"
                badge_label = "⚡ HIGH-TIGHT DISTRIBUTION (STAGE-2 BREAKDOWN)"
            elif is_coiling_launchpad:
                state_label = "PRE_IGNITION_SHORT"
                badge_label = "⚡ PRE-IGNITION LAUNCHPAD (EARLY SHORT)"
            elif is_fresh_breakout:
                state_label = "BEARISH_IGNITION"
                badge_label = "📉 FRESH BREAKDOWN SHORT (TRIGGER CONFIRMED)"
            elif dist_to_pivot_pct < -1.5 and base_width_pct > 1.5:
                state_label = "AWAITING_BASE_FORMATION"
                badge_label = "WAIT FOR BASE / VWAP PULLBACK"
                pts = min(pts, 48)

        return {
            "score": max(0, min(100, pts)),
            "state_label": state_label,
            "badge_label": badge_label,
            "is_launchpad": is_coiling_launchpad or is_continuation_runner or is_fresh_breakout,
            "is_continuation": is_continuation_runner,
            "base_high": round(base_high, 2),
            "base_low": round(base_low, 2),
            "base_width_pct": base_width_pct,
            "dist_to_pivot_pct": dist_to_pivot_pct,
            "trigger_price": trigger_price,
            "tight_sl": tight_sl,
            "target_price": target_price,
            "rs_spread": rs_spread,
            "is_vcp_contracting": is_vcp_contracting,
            "is_volume_dry": is_volume_dry,
            "reasons": reasons,
            "orderflow_buy_pct": buy_pct
        }

    @staticmethod
    def score_swing_stage2(eval_res: Dict[str, Any], meta: Dict[str, Any]) -> Dict[str, Any]:
        """
        Computes 0-100 Swing Trading Score (Stage-2 Minervini Trend Template):
        - 30 pts: Price > 50 DMA and 50 DMA slope > 0
        - 25 pts: Multi-week base contraction (VCP 3-5% near resistance)
        - 25 pts: Momentum ranking (from excel/data)
        - 20 pts: Volume contraction on pullbacks
        """
        pts = 0
        reasons = []

        last_price = eval_res.get("last_price", 0.0)
        ema50 = eval_res.get("ema50", last_price)
        momentum_score = float(meta.get("momentum_score", 50.0) or 50.0)

        # 1. Stage-2 Alignment (35 pts)
        if last_price > ema50:
            pts += 35
            reasons.append("Stage-2 Trend (Above 50 DMA)")

        # 2. Momentum Metric (30 pts)
        if momentum_score >= 70:
            pts += 30
            reasons.append(f"Top Decile Momentum ({momentum_score:.0f}/100)")
        elif momentum_score >= 50:
            pts += 20
            reasons.append(f"Solid Momentum ({momentum_score:.0f}/100)")

        # 3. Base Tightness (25 pts)
        if eval_res.get("is_coiling"):
            pts += 25
            reasons.append("Multi-Day VCP Compression")
        elif eval_res.get("signal_direction") == 1:
            pts += 20
            reasons.append("Early Expansion")

        # 4. Liquidity & Vol (10 pts)
        if eval_res.get("rvol", 1.0) >= 1.2:
            pts += 10

        return {
            "score": min(100, pts),
            "reasons": reasons
        }

    @staticmethod
    def score_long_term_compounder(meta: Dict[str, Any]) -> Dict[str, Any]:
        """
        Computes 0-100 Long-Term Wealth Compounder Score:
        - 40 pts: Durability Score (Clean balance sheet, institutional backing)
        - 35 pts: Valuation Score (P/E margin of safety)
        - 25 pts: Positive Rank & Low Threat
        """
        durability = float(meta.get("durability", 50.0) or 50.0)
        valuation = float(meta.get("valuation", 50.0) or 50.0)
        total_rank = float(meta.get("rank_total", 50.0) or 50.0)

        pts = 0
        reasons = []

        if durability >= 70:
            pts += 40
            reasons.append(f"High Durability ({durability:.0f}/100)")
        elif durability >= 50:
            pts += 25

        if valuation >= 50:
            pts += 35
            reasons.append(f"Attractive Valuation ({valuation:.0f}/100)")
        elif valuation >= 35:
            pts += 20

        if total_rank >= 80:
            pts += 25
            reasons.append(f"Rank Alpha ({total_rank:.0f})")
        else:
            pts += 15

        return {
            "score": min(100, pts),
            "reasons": reasons
        }

    @staticmethod
    def score_penny_runner(eval_res: Dict[str, Any], meta: Dict[str, Any]) -> Dict[str, Any]:
        """
        Computes 0-100 Penny / High-Beta Runner Score:
        - Must be < ₹100
        - 40 pts: Explosive Volume Surge (RVOL >= 2.0x)
        - 30 pts: Breakout above consolidation ceiling
        - 30 pts: Anti-Circuit Safe (Liquid order book, not trapped)
        """
        last_price = eval_res.get("last_price", 0.0)
        if last_price > 120.0:
            return {"score": 0, "reasons": ["Price > ₹100 (Non-Penny)"]}

        pts = 0
        reasons = []

        rvol = eval_res.get("rvol", 1.0)
        if rvol >= 2.5:
            pts += 40
            reasons.append(f"Massive Volume Surge ({rvol}x RVOL)")
        elif rvol >= 1.5:
            pts += 25

        if eval_res.get("signal_direction") == 1:
            pts += 30
            reasons.append("Clean Ceiling Breakout")
        elif eval_res.get("is_coiling"):
            pts += 20
            reasons.append("Penny Squeeze")

        if eval_res.get("anti_circuit_status") == "SAFE":
            pts += 30
            reasons.append("Liquid Order Book (No Circuit Trap)")
        else:
            pts -= 20

        return {
            "score": max(0, min(100, pts)),
            "reasons": reasons
        }

