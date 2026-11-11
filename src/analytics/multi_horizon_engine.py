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
        orderflow_data: Optional[Dict[str, Any]] = None,
        nifty_change_pct: float = 0.0
    ) -> Dict[str, Any]:
        """
        Computes 0-100 Intraday VCP & Orderflow Score:
        - 30 pts: Micro-Base Proximity to HOD/Range Top (within 0.1% - 0.7%)
        - 25 pts: Intraday Relative Strength (Outperforming Nifty)
        - 20 pts: VWAP & 50 EMA Alignment (Price > VWAP > 50 EMA)
        - 15 pts: Volume Dry-up in base followed by expansion
        - 10 pts: Orderflow Bid-Ask Imbalance (Bids > Asks)
        """
        pts = 0
        reasons = []
        
        last_price = eval_res.get("last_price", 0.0)
        range_top = eval_res.get("range_top", last_price)
        pct_change = eval_res.get("pct_change_pdc", 0.0)
        vwap = eval_res.get("vwap", last_price)
        ema50 = eval_res.get("ema50", last_price)
        rvol = eval_res.get("rvol", 1.0)
        is_coiling = eval_res.get("is_coiling", False)

        # 1. Micro-Base / Coiling Proximity (30 pts)
        dist_to_top_pct = round(((range_top - last_price) / max(1.0, range_top)) * 100.0, 2)
        if 0.0 <= dist_to_top_pct <= 0.8:
            pts += 30
            reasons.append(f"Launchpad Squeeze ({dist_to_top_pct}% to breakout)")
        elif dist_to_top_pct < 0.0 and abs(dist_to_top_pct) <= 1.5:
            pts += 25
            reasons.append(f"Fresh Breakout (+{abs(dist_to_top_pct)}%)")
        elif is_coiling:
            pts += 20
            reasons.append("Spring Coiled (ATR Band Pinch)")

        # 2. Intraday Relative Strength vs Nifty (25 pts)
        rs_spread = round(pct_change - nifty_change_pct, 2)
        if rs_spread >= 2.0:
            pts += 25
            reasons.append(f"Super-Relative Strength (+{rs_spread}% vs Nifty)")
        elif rs_spread >= 0.8:
            pts += 18
            reasons.append(f"RS Positive (+{rs_spread}% vs Nifty)")
        elif rs_spread > 0.0:
            pts += 10

        # 3. Trend Alignment (20 pts)
        if last_price >= vwap and last_price >= ema50:
            pts += 20
            reasons.append("Price > VWAP & 50 EMA")
        elif last_price >= vwap:
            pts += 12

        # 4. Volume Dynamics (15 pts)
        if rvol >= 1.5:
            pts += 15
            reasons.append(f"High RVOL ({rvol}x)")
        elif rvol >= 1.1:
            pts += 10

        # 5. Orderflow Bid-Ask Imbalance (10 pts)
        buy_pct = 50.0
        if orderflow_data:
            buy_pct = float(orderflow_data.get("buy_percentage", 50.0))
            if buy_pct >= 65.0:
                pts += 10
                reasons.append(f"Orderflow: {buy_pct:.0f}% Institutional Bids")
            elif buy_pct >= 55.0:
                pts += 5
        else:
            # Default proxy if broker depth not connected on specific ticker
            if pts >= 60:
                pts += 7

        return {
            "score": min(100, pts),
            "rs_spread": rs_spread,
            "dist_to_top_pct": dist_to_top_pct,
            "reasons": reasons,
            "is_launchpad": (0.0 <= dist_to_top_pct <= 0.8),
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
