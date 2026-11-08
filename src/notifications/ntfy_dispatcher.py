"""
FinPlus+ NTFY Alert Dispatcher
Transmits Early Ignition and Volatility Coiling Signals to https://ntfy.sh/Finplus-Delta
"""

import time
import urllib.request
import json
from typing import Dict, Any, List
from src.utils.time_and_logger import setup_logger, get_utc_iso, get_ist_time_str

logger = setup_logger("NTFYDispatcher")

NTFY_TOPICS = ["Finplus-Delta", "Finplus-Breadth"]

class NTFYDispatcher:
    def __init__(self, topics: List[str] = NTFY_TOPICS):
        self.topics = topics
        self._sent_cache = {} # (symbol, state) -> timestamp

    def send_early_ignition_alert(self, scan_result: Dict[str, Any], regime_confluence: str, regime_badge: str) -> bool:
        """
        Dispatches high-conviction Early Ignition strike alert with actionable options trade setup.
        """
        symbol = scan_result.get("symbol")
        state = scan_result.get("ignition_state")
        now = time.time()
        
        # 30-min deduplication per symbol/state
        cache_key = (symbol, state)
        if cache_key in self._sent_cache:
            if (now - self._sent_cache[cache_key]) < 1800:
                return False
                
        self._sent_cache[cache_key] = now
        
        direction = scan_result.get("signal_direction", 0)
        is_bullish = direction == 1
        
        title_icon = "🟢" if is_bullish else "🔴"
        action_type = "CALL OPTION IGNITION" if is_bullish else "PUT OPTION BREAKDOWN"
        
        contract = scan_result.get("recommended_contract", "N/A")
        last_price = scan_result.get("last_price")
        pct_change = scan_result.get("pct_change_pdc")
        pdh = scan_result.get("pdh")
        pdl = scan_result.get("pdl")
        risk = scan_result.get("risk_management", {})
        importance = scan_result.get("importance", "HIGH")
        category = scan_result.get("category", "STOCK")
        
        priority = 5 if importance == "HIGH" else 4
        
        # Build Markdown Message
        lines = [
            f"{title_icon} **FINPLUS+ {action_type}** {title_icon}",
            f"**Ticker:** `{symbol}` ({category}) | **IST:** `{get_ist_time_str()}`",
            f"**Spot LTP:** `₹{last_price}` (Early Move: `{pct_change:+0.2f}%`)",
            f"**Key Levels:** PDH: `₹{pdh}` | PDL: `₹{pdl}`",
            "---",
            f"🎯 **RECOMMENDED CONTRACT:** `{contract}`",
        ]
        
        if "premium_stop_loss_pct" in risk:
            lines.append(f"🛑 **Option SL:** `{risk['premium_stop_loss_pct']}% of Premium` | 🎯 **Target:** `{risk.get('premium_target_pct', 150)}%+`")
            if "capital_guidance" in risk:
                lines.append(f"💡 *{risk['capital_guidance']}*")
        elif "underlying_sl" in risk:
            lines.append(f"🛑 **Underlying SL:** `₹{risk['underlying_sl']}` | 🎯 **Target:** `₹{risk['underlying_target']}` (R:R {risk.get('rr_ratio', '1:2')})")
            lines.append(f"🛡️ **Option SL:** `25% Premium Cut` if underlying hits SL")
            
        lines.append("---")
        lines.append(f"🏛️ **Air Traffic Control (App #1):** `{regime_badge}`")
        if "VETOED" in regime_confluence:
            lines.append("⚠️ **WARNING:** Macro market regime indicates high false breakout risk. Exercise extreme caution.")
        else:
            lines.append("✅ **CONFLUENCE VERIFIED:** Aligned with broader market trend & breadth.")
            
        message = "\n".join(lines)
        title = f"{title_icon} {symbol}: {action_type} ({contract})"
        
        # Dispatch to all topics
        success = False
        for topic in self.topics:
            url = f"https://ntfy.sh/{topic}"
            headers = {
                "Title": title.encode('utf-8'),
                "Priority": str(priority),
                "Tags": "chart_with_upwards_trend,dart,moneybag" if is_bullish else "chart_with_downwards_trend,rotating_light,fire",
                "Actions": "view, Open Scanner Cockpit, http://127.0.0.1:8001"
            }
            try:
                req = urllib.request.Request(url, data=message.encode('utf-8'), headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=5) as resp:
                    if resp.status == 200:
                        logger.info(f"NTFY alert successfully delivered to topic '{topic}' for {symbol}")
                        success = True
            except Exception as e:
                logger.error(f"Failed delivering NTFY alert to '{topic}': {e}")
                
        return success

    def send_coiling_alert(self, scan_result: Dict[str, Any]) -> bool:
        """Sends early alert that a stock is coiled in a tight 20-candle range ready for ignition."""
        symbol = scan_result.get("symbol")
        now = time.time()
        cache_key = (symbol, "COILING")
        if cache_key in self._sent_cache:
            if (now - self._sent_cache[cache_key]) < 3600: # 1-hour deduplication
                return False
                
        self._sent_cache[cache_key] = now
        
        last_price = scan_result.get("last_price")
        range_top = scan_result.get("range_top")
        range_bot = scan_result.get("range_bottom")
        pdh = scan_result.get("pdh")
        pdl = scan_result.get("pdl")
        
        message = (
            f"⚡ **VOLATILITY COILING DETECTED (PRE-IGNITION)** ⚡\n"
            f"**Ticker:** `{symbol}` | **Spot:** `₹{last_price}`\n"
            f"Price compressed inside 20-period LuxAlgo band:\n"
            f"• **Range Top:** `₹{range_top}` (PDH: `₹{pdh}`)\n"
            f"• **Range Bottom:** `₹{range_bot}` (PDL: `₹{pdl}`)\n"
            f"Prepare for explosive expansion once either level breaks!"
        )
        
        headers = {
            "Title": f"⚡ Coiling Watch: {symbol} in Tight Squeeze".encode('utf-8'),
            "Priority": "3",
            "Tags": "hourglass_flowing_sand,zap",
            "Actions": "view, Open Scanner Cockpit, http://127.0.0.1:8001"
        }
        
        for topic in self.topics:
            try:
                req = urllib.request.Request(f"https://ntfy.sh/{topic}", data=message.encode('utf-8'), headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=5):
                    pass
            except Exception:
                pass
        return True
