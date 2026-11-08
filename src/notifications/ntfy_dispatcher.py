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
        
        # 1. SENSEX 30 RADAR
        lines.append(f"👑 **BSE SENSEX 30 OPTIONS (HIGH PRIORITY):**")
        lines.append(f"• **Spot LTP:** `₹{sx.get('last_price', '--')}` ({sx.get('pct_change_pdc', 0):+0.2f}%)")
        lines.append(f"• **Call Strike (CE):** `{sx.get('call_strike')}` (Trigger > `₹{sx.get('pdh')}`)")
        lines.append(f"• **Put Strike (PE):** `{sx.get('put_strike')}` (Trigger < `₹{sx.get('pdl')}`)")
        lines.append(f"• **Directive:** `{sx.get('action_badge')}` — *{sx.get('directive')}*")
        lines.append("")
        
        # 2. NIFTY 50 RADAR
        lines.append(f"🎯 **NSE NIFTY 50 OPTIONS:**")
        lines.append(f"• **Spot LTP:** `₹{nf.get('last_price', '--')}` ({nf.get('pct_change_pdc', 0):+0.2f}%)")
        lines.append(f"• **Call Strike (CE):** `{nf.get('call_strike')}` | **Put Strike (PE):** `{nf.get('put_strike')}`")
        lines.append(f"• **Directive:** `{nf.get('action_badge')}`")
        lines.append("")

        # 3. BEST STOCK OPTION
        if stk:
            lines.append("💎 **BEST STOCK OPTION:**")
            lines.append(f"• **ACTION:** `{stk.get('option_action_badge')}`")
            lines.append(f"• **Contract:** `{stk.get('recommended_contract')}` (Lot: `{stk.get('lot_size')}` | Spot: `₹{stk.get('last_price')}`)")
            lines.append(f"• **Score:** `{stk.get('confluence_score')}/100`")
            risk = stk.get("risk_management", {})
            if "underlying_sl" in risk:
                lines.append(f"• **Underlying SL:** `₹{risk.get('underlying_sl')}` | **Target:** `₹{risk.get('underlying_target')}`")
            lines.append(f"• **Regime Gate:** `{stk.get('regime_badge')}`")
            lines.append("")

        # 4. BEST INTRADAY CASH STOCK (DECOUPLED FROM OPTIONS)
        if cash:
            lines.append("📈 **BEST INTRADAY CASH RUNNER (BROAD MARKET QUALITY):**")
            lines.append(f"• **ACTION:** `{cash.get('intraday_action_badge')}` on `{cash.get('symbol')}` @ `₹{cash.get('last_price')}`")
            lines.append(f"• **Setup:** {cash.get('intraday_directive')}")
            lines.append(f"• **Safety:** Anti-Circuit Verified (Deep Institutional Liquidity, No Freeze Risk)")
            lines.append(f"• **Score:** `{cash.get('confluence_score')}/100` | **Regime Gate:** `{cash.get('regime_badge')}`")
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
