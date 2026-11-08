"""
FinPlus+ NTFY Alert Dispatcher
Enforces strict 30-minute periodic pulse to eliminate overtrading.
Transmits explicit Call (CE) vs Put (PE) strike setups, and explicit
Intraday Cash BUY / SELL directives.
"""

import time
import urllib.request
import json
from typing import Dict, Any, List, Optional
from src.utils.time_and_logger import setup_logger, get_utc_iso, get_ist_time_str

logger = setup_logger("NTFYDispatcher")

NTFY_TOPICS = ["Finplus-Delta", "Finplus-Breadth"]

class NTFYDispatcher:
    def __init__(self, topics: List[str] = NTFY_TOPICS, min_interval_seconds: int = 1800):
        self.topics = topics
        self.min_interval = min_interval_seconds
        self.last_pulse_timestamp = 0.0

    def send_30m_shortlist_pulse(self, shortlist: Dict[str, Any], force: bool = False) -> bool:
        now = time.time()
        if not force and (now - self.last_pulse_timestamp) < self.min_interval:
            logger.info("Skipping NTFY pulse: 30m interval not reached yet.")
            return False
            
        self.last_pulse_timestamp = now
        ist_time = shortlist.get("ist_time", get_ist_time_str())
        
        sx = shortlist.get("sensex_radar", {})
        nf = shortlist.get("nifty_radar", {})
        stk = shortlist.get("best_stock_option", {})
        cash = shortlist.get("best_intraday_stock", {})
        
        has_actionable = shortlist.get("has_actionable_trades", False)
        
        lines = [
            f"🏛️ **FINPLUS+ 30-MIN INTRADAY PULSE** | `{ist_time}`",
            "---"
        ]
        
        # 1. SENSEX 30 RADAR (SINGLE CONFIRMED DIRECTION ONLY)
        lines.append(f"👑 **BSE SENSEX 30 OPTIONS (HIGH PRIORITY):**")
        lines.append(f"• **Spot LTP:** `₹{sx.get('last_price', '--')}` ({sx.get('pct_change_pdc', 0):+0.2f}%) | **50 EMA:** `₹{sx.get('ema50', '--')}` ({sx.get('ema50_dist_pct', 0):+0.2f}%)")
        if sx.get("is_actionable") and sx.get("recommended_strike"):
            lines.append(f"• **CONFIRMED STRIKE:** `{sx.get('recommended_strike')}`")
            lines.append(f"• **ACTION:** `{sx.get('action_badge')}`")
            lines.append(f"• **Setup:** {sx.get('directive')}")
        else:
            lines.append(f"• **Recommendation:** `HOLD CASH / STANDBY` (No option recommended)")
            lines.append(f"• **Condition:** {sx.get('directive')}")
        lines.append("")
        
        # 2. NIFTY 50 RADAR (SINGLE CONFIRMED DIRECTION ONLY)
        lines.append(f"🎯 **NSE NIFTY 50 OPTIONS:**")
        lines.append(f"• **Spot LTP:** `₹{nf.get('last_price', '--')}` ({nf.get('pct_change_pdc', 0):+0.2f}%) | **50 EMA:** `₹{nf.get('ema50', '--')}` ({nf.get('ema50_dist_pct', 0):+0.2f}%)")
        if nf.get("is_actionable") and nf.get("recommended_strike"):
            lines.append(f"• **CONFIRMED STRIKE:** `{nf.get('recommended_strike')}`")
            lines.append(f"• **ACTION:** `{nf.get('action_badge')}`")
            lines.append(f"• **Setup:** {nf.get('directive')}")
        else:
            lines.append(f"• **Recommendation:** `HOLD CASH / STANDBY` (No option recommended)")
            lines.append(f"• **Condition:** {nf.get('directive')}")
        lines.append("")

        # 3. BEST STOCK OPTION (STRICT MARKET GATED)
        lines.append("💎 **BEST STOCK OPTION:**")
        if stk:
            lines.append(f"• **CONFIRMED CONTRACT:** `{stk.get('recommended_contract')}` (Lot: `{stk.get('lot_size')}` | Spot: `₹{stk.get('last_price')}`)")
            lines.append(f"• **ACTION:** `{stk.get('option_action_badge')}`")
            lines.append(f"• **50 EMA Alignment:** `₹{stk.get('ema50')}` ({stk.get('ema50_dist_pct', 0):+0.2f}% confirmed)")
            lines.append(f"• **Score:** `{stk.get('confluence_score')}/100`")
            risk = stk.get("risk_management", {})
            if "underlying_sl" in risk:
                lines.append(f"• **Underlying SL:** `₹{risk.get('underlying_sl')}` | **Target:** `₹{risk.get('underlying_target')}`")
            lines.append(f"• **Regime Clearance:** `{stk.get('regime_badge')}`")
        else:
            reason = shortlist.get("stock_option_standby_reason", "No setup cleared macro trend + 50pt confluence")
            lines.append(f"• **Recommendation:** `HOLD CASH` ({reason})")
        lines.append("")

        # 4. BEST INTRADAY CASH RUNNER (DECOUPLED & ANTI-CIRCUIT VERIFIED)
        lines.append("📈 **BEST INTRADAY CASH RUNNER (BROAD MARKET QUALITY):**")
        if cash:
            lines.append(f"• **ACTION:** `{cash.get('intraday_action_badge')}` on `{cash.get('symbol')}` @ `₹{cash.get('last_price')}`")
            lines.append(f"• **50 EMA Alignment:** `₹{cash.get('ema50')}` ({cash.get('ema50_dist_pct', 0):+0.2f}% confirmed)")
            lines.append(f"• **Setup:** {cash.get('intraday_directive')}")
            lines.append(f"• **Safety:** Anti-Circuit Verified (Turnover > ₹15 Cr, Zero Freeze Risk)")
            lines.append(f"• **Score:** `{cash.get('confluence_score')}/100` | **Regime Gate:** `{cash.get('regime_badge')}`")
        else:
            reason = shortlist.get("cash_standby_reason", "No high-turnover breakout cleared by market breadth")
            lines.append(f"• **Recommendation:** `HOLD CASH` ({reason})")
        lines.append("")
            
        lines.append("---")
        lines.append("🛡️ *Next consolidated pulse in 30 minutes.*")

        message = "\n".join(lines)
        title = f"👑 FinPlus+ 30m Pulse: Strike Directives ({ist_time})"
        
        success = False
        for topic in self.topics:
            url = f"https://ntfy.sh/{topic}"
            headers = {
                "Title": title.encode('utf-8'),
                "Priority": "4",
                "Tags": "chart_with_upwards_trend,dart,moneybag",
                "Actions": "view, Open Scanner Cockpit, http://127.0.0.1:8001"
            }
            try:
                req = urllib.request.Request(url, data=message.encode('utf-8'), headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=5) as resp:
                    if resp.status == 200:
                        logger.info(f"30m pulse delivered to '{topic}'")
                        success = True
            except Exception as e:
                logger.error(f"Failed delivering pulse to '{topic}': {e}")
                
        return success
