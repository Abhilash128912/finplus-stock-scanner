"""
FinPlus+ NTFY Alert Dispatcher
Enforces strict 30-minute periodic pulse to eliminate overtrading.
Transmits only the #1 Best-in-Class selections with full points breakdown,
supporting "TREND CONTINUATION" updates.
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
        self.min_interval = min_interval_seconds # 30 minutes
        self.last_pulse_timestamp = 0.0

    def send_30m_shortlist_pulse(self, shortlist: Dict[str, Any], force: bool = False) -> bool:
        """
        Dispatches consolidated 30-minute pulse with the best index and stock options.
        Enforces strict 30-minute cooldown to prevent overtrading.
        """
        now = time.time()
        if not force and (now - self.last_pulse_timestamp) < self.min_interval:
            logger.info(f"Skipping NTFY pulse: 30m interval not reached yet ({int(now - self.last_pulse_timestamp)}s / {self.min_interval}s).")
            return False
            
        self.last_pulse_timestamp = now
        ist_time = shortlist.get("ist_time", get_ist_time_str())
        
        best_index = shortlist.get("best_index_option")
        best_stock = shortlist.get("best_stock_option")
        
        has_actionable = shortlist.get("has_actionable_trades", False)
        
        # Build Markdown Message
        lines = [
            f"🏛️ **FINPLUS+ 30-MIN INTRADAY PULSE** | `{ist_time}`",
            "---"
        ]
        
        if not has_actionable:
            lines.append("🛑 **RECOMMENDATION: HOLD 100% CASH / SIT OUT**")
            lines.append("No index or stock option cleared the 50+ score threshold with confirmed macro trend.")
            lines.append("Preserve capital — zero trades taken this 30-minute window.")
            title = f"🛑 FinPlus+ 30m Pulse: Cash Mode ({ist_time})"
            priority = 3
            tags = "shield,hand"
        else:
            priority = 4
            tags = "dart,gem,chart_with_upwards_trend"
            title = f"👑 FinPlus+ 30m Picks: Best Setups ({ist_time})"
            
            # 1. Best Index Option
            if best_index:
                is_cont = best_index.get("is_continuation", False)
                icon = "🔄" if is_cont else "🎯"
                prefix = "**TREND CONTINUATION**" if is_cont else "**NEW STRIKE ENTRY**"
                score = best_index.get("confluence_score", 0)
                pts = best_index.get("points_breakdown", {})
                risk = best_index.get("risk_management", {})
                
                lines.append(f"{icon} **BEST INDEX OPTION ({prefix}):**")
                lines.append(f"• **Contract:** `{best_index.get('recommended_contract')}` (Spot: `₹{best_index.get('last_price')}`)")
                lines.append(f"• **Score:** `{score}/100` [Range: {pts.get('range_breakout_pts', 0)} | PDH/PDL: {pts.get('pdh_pdl_pts', 0)} | VWAP: {pts.get('vwap_pts', 0)} | Vol: {pts.get('volume_pts', 0)}]")
                lines.append(f"• **SL:** `{risk.get('premium_stop_loss_pct', 45)}% Premium` | **Target:** `{risk.get('premium_target_pct', 180)}%+`")
                if is_cont:
                    lines.append("• 💡 *Previous trade holding strong. Trail SL to breakeven or protect profits.*")
                lines.append("")

            # 2. Best Stock Option
            if best_stock:
                is_cont = best_stock.get("is_continuation", False)
                icon = "🔄" if is_cont else "💎"
                prefix = "**TREND CONTINUATION**" if is_cont else "**NEW STRIKE ENTRY**"
                score = best_stock.get("confluence_score", 0)
                pts = best_stock.get("points_breakdown", {})
                risk = best_stock.get("risk_management", {})
                
                lines.append(f"{icon} **BEST STOCK OPTION ({prefix}):**")
                lines.append(f"• **Stock:** `{best_stock.get('symbol')}` (Lot: `{best_stock.get('lot_size')}`)")
                lines.append(f"• **Contract:** `{best_stock.get('recommended_contract')}` (Spot: `₹{best_stock.get('last_price')}` | Early Move: `{best_stock.get('pct_change_pdc'):+0.2f}%`)")
                lines.append(f"• **Score:** `{score}/100` [Range: {pts.get('range_breakout_pts', 0)} | PDH/PDL: {pts.get('pdh_pdl_pts', 0)} | VWAP: {pts.get('vwap_pts', 0)} | Vol: {pts.get('volume_pts', 0)}]")
                lines.append(f"• **Underlying SL:** `₹{risk.get('underlying_sl')}` | **Target:** `₹{risk.get('underlying_target')}`")
                lines.append(f"• **Option Risk:** `25% Premium Cut` on stop trigger")
                if is_cont:
                    lines.append("• 💡 *Previous trade holding strong. Trail SL to lock in gains.*")
                lines.append("")

            lines.append("---")
            lines.append("🛡️ *Strict 30-min window enforced to prevent overtrading. Next update in 30 minutes.*")

        message = "\n".join(lines)
        
        # Dispatch to all topics
        success = False
        for topic in self.topics:
            url = f"https://ntfy.sh/{topic}"
            headers = {
                "Title": title.encode('utf-8'),
                "Priority": str(priority),
                "Tags": tags,
                "Actions": "view, Open Scanner Cockpit, http://127.0.0.1:8001"
            }
            try:
                req = urllib.request.Request(url, data=message.encode('utf-8'), headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=5) as resp:
                    if resp.status == 200:
                        logger.info(f"30m shortlist pulse successfully delivered to '{topic}'")
                        success = True
            except Exception as e:
                logger.error(f"Failed delivering 30m pulse to '{topic}': {e}")
                
        return success
