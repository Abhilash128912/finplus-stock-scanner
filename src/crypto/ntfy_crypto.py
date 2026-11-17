"""
FinPlus+ Crypto Delta - Central Notification Dispatcher
Delivers priority trade ignition, order execution, and trailing stop alerts to Finplus-Delta.
"""

import os
import time
import requests
from typing import Dict, Any, Optional
from src.utils.time_and_logger import setup_logger, get_ist_now
from src.crypto.crypto_config import NTFY_URL, NTFY_TOPIC, ENABLE_DELTA_NTFY

logger = setup_logger("CryptoNTFYDispatcher")

def safe_headers(headers: Dict[str, Any]) -> Dict[str, str]:
    """Forces ASCII on all HTTP header values. Non-ASCII chars (emojis) are stripped."""
    clean = {}
    for k, v in headers.items():
        if isinstance(v, str):
            clean[k] = v.encode("ascii", "ignore").decode("ascii").strip()
        else:
            clean[k] = str(v)
    return clean

class CryptoNTFYDispatcher:
    _last_suppress_log_ts: float = 0.0
    _recent_alert_cache: Dict[str, float] = {}

    @classmethod
    def _safe_post(cls, url: str, message: str, headers: Dict[str, str]) -> bool:
        """
        Shared central send method.
        Dispatches to NTFY if ENABLE_DELTA_NTFY is True or if running in Render cloud.
        Applies a 30-minute deduplication window to prevent alert flooding.
        """
        is_render = bool(os.getenv("RENDER") or os.getenv("RENDER_SERVICE_ID"))
        allowed = ENABLE_DELTA_NTFY or is_render

        if not allowed:
            now_ts = time.time()
            if (now_ts - cls._last_suppress_log_ts) >= 3600:
                cls._last_suppress_log_ts = now_ts
                logger.info("NTFY suppressed (ENABLE_DELTA_NTFY is false and not on Render)")
            return False

        # 30-minute content deduplication
        import hashlib
        alert_hash = hashlib.sha256(f"{headers.get('Title', '')}_{message[:150]}".encode("utf-8")).hexdigest()
        now_ts = time.time()
        last_sent = cls._recent_alert_cache.get(alert_hash, 0.0)
        if (now_ts - last_sent) < 1800.0:
            logger.info(f"NTFY alert deduped (sent {int(now_ts - last_sent)}s ago): {headers.get('Title')}")
            return False
        cls._recent_alert_cache[alert_hash] = now_ts

        clean_hdrs = safe_headers(headers)
        try:
            resp = requests.post(url, data=message.encode("utf-8"), headers=clean_hdrs, timeout=10)
            if resp.status_code == 200:
                logger.info(f"Notification delivered successfully to {url}")
                return True
            elif resp.status_code == 429:
                logger.warning(f"NTFY rate limit reached (HTTP 429) for {url}")
            else:
                logger.error(f"NTFY returned HTTP {resp.status_code}: {resp.text[:100]}")
        except Exception as e:
            logger.error(f"Error dispatching notification to NTFY: {e}")

        return False

    def send_order_opened_alert(self, pos: Dict[str, Any]) -> bool:
        """
        Dispatches an immediate high-priority alert whenever a Delta Trade (Buy/Long or Sell/Short) is executed.
        """
        sym = pos.get("symbol", "")
        action = pos.get("action", "")
        entry = pos.get("entry_price", 0.0)
        sl = pos.get("current_sl", 0.0)
        t1 = pos.get("target_1", 0.0)
        t2 = pos.get("target_2", 0.0)
        qty = pos.get("quantity", 1.0)
        notional = pos.get("notional_usd", 0.0)
        notes = pos.get("notes", "Delta Market Order")

        is_buy = "BUY" in action or "LONG" in action
        icon = "🟢" if is_buy else "🔴"
        action_title = "DELTA BUY (LONG)" if is_buy else "DELTA SELL (SHORT)"

        lines = [
            f"{icon} [{action_title}] {sym}",
            "═" * 40,
            f"• Entry Price: ${entry:,.2f}",
            f"• Stop Loss: ${sl:,.2f}",
            f"• Target 1: ${t1:,.2f}",
            f"• Target 2: ${t2:,.2f}",
            f"• Position Size: {qty} (Notional: ${notional:,.2f})",
            f"• Setup Context: {notes}",
            "═" * 40,
            f"Platform: Delta Exchange India (Perpetual Swaps)",
            f"Time: {get_ist_now().strftime('%I:%M:%S %p IST')}"
        ]
        msg = "\n".join(lines)

        headers = {
            "Title": f"{icon} {action_title} {sym} @ ${entry:,.2f}",
            "Priority": "urgent" if is_buy else "high",
            "Tags": "zap,chart_with_upwards_trend,moneybag" if is_buy else "zap,chart_with_downwards_trend,warning",
            "X-Markdown": "yes",
            "Click": "http://127.0.0.1:8005"
        }
        return self._safe_post(NTFY_URL, msg, headers)

    def send_breakeven_trailed_alert(self, pos: Dict[str, Any]) -> bool:
        """Dispatches an alert when SL is moved to Breakeven (Zero Risk)."""
        sym = pos.get("symbol", "")
        entry = pos.get("entry_price", 0.0)
        curr_p = pos.get("current_price", 0.0)
        t1 = pos.get("target_1", 0.0)

        lines = [
            f"🔒 [STOP-LOSS TRAILED TO BREAKEVEN] {sym}",
            "═" * 40,
            f"• 50% distance to Target 1 achieved!",
            f"• Current Price: ${curr_p:,.2f}",
            f"• Trailed SL: ${entry:,.2f} (Entry Level)",
            f"• Target 1: ${t1:,.2f}",
            f"• Status: Capital Protected • Trade is now 100% RISK-FREE",
            "═" * 40,
            f"Time: {get_ist_now().strftime('%I:%M:%S %p IST')}"
        ]
        msg = "\n".join(lines)

        headers = {
            "Title": f"🔒 {sym} SL Trailed to Breakeven (${entry:,.2f})",
            "Priority": "default",
            "Tags": "shield,lock",
            "X-Markdown": "yes",
            "Click": "http://127.0.0.1:8005"
        }
        return self._safe_post(NTFY_URL, msg, headers)

    def send_breakout_alert(self, signal: Dict[str, Any]) -> bool:
        """Dispatches real-time priority ignition alert for a confirmed breakout."""
        sym = signal.get("symbol", "")
        action = signal.get("action", "")
        confluence = signal.get("confluence_score", 0)
        ltp = signal.get("ltp", 0.0)
        sl = signal.get("stop_loss", 0.0)
        t1 = signal.get("target_1", 0.0)
        t2 = signal.get("target_2", 0.0)
        reason = signal.get("reason", "")
        box_w = signal.get("box_width_pct", 0.0)
        vol = signal.get("vol_ratio", 1.0)

        is_long = "BUY" in action or "LONG" in action
        icon = "🚀" if is_long else "🔻"
        dir_label = "LONG IGNITION (BUY)" if is_long else "SHORT IGNITION (SELL)"

        lines = [
            f"{icon} [DELTA BREAKOUT RADAR] {sym} — {dir_label}",
            "═" * 40,
            f"• Confluence: {confluence}/100 | Squeeze Box: {box_w:.2f}% | Vol: {vol}x",
            f"• Trigger Price (LTP): ${ltp:,.2f}",
            f"• Stop Loss: ${sl:,.2f}",
            f"• Target 1: ${t1:,.2f}",
            f"• Target 2: ${t2:,.2f}",
            "═" * 40,
            f"Setup Rationale: {reason}",
            f"Exchange: Delta Exchange India (Perpetual Futures)",
            f"Time: {get_ist_now().strftime('%I:%M:%S %p IST')}"
        ]
        msg = "\n".join(lines)

        headers = {
            "Title": f"{icon} {sym} {dir_label} | {confluence} pts",
            "Priority": "high",
            "Tags": "zap,crypto,chart_with_upwards_trend" if is_long else "zap,crypto,chart_with_downwards_trend",
            "X-Markdown": "yes",
            "Click": "http://127.0.0.1:8005"
        }
        return self._safe_post(NTFY_URL, msg, headers)

    def send_trade_closed_alert(self, trade: Dict[str, Any]) -> bool:
        """Dispatches alert when paper trade hits SL or Target."""
        sym = trade.get("symbol", "")
        action = trade.get("action", "")
        pnl_usd = trade.get("pnl_usd", 0.0)
        pnl_pct = trade.get("pnl_pct", 0.0)
        exit_reason = trade.get("exit_reason", "")
        entry = trade.get("entry_price", 0.0)
        exit_p = trade.get("exit_price", 0.0)

        is_win = pnl_usd >= 0
        icon = "🎯" if is_win else "🛑"
        pnl_str = f"+${pnl_usd:,.2f}" if is_win else f"-${abs(pnl_usd):,.2f}"

        lines = [
            f"{icon} [DELTA TRADE CLOSED] {sym} ({action})",
            "═" * 40,
            f"• Outcome: {'PROFIT' if is_win else 'STOP-LOSS'} ({exit_reason})",
            f"• Realized PnL: {pnl_str} ({pnl_pct:+.2f}%)",
            f"• Entry: ${entry:,.2f} ➔ Exit: ${exit_p:,.2f}",
            "═" * 40,
            f"Portfolio Engine: FinPlus+ Crypto Paper Tracker",
            f"Time: {get_ist_now().strftime('%I:%M:%S %p IST')}"
        ]
        msg = "\n".join(lines)

        headers = {
            "Title": f"{icon} {sym} Closed: {pnl_str} ({exit_reason})",
            "Priority": "default",
            "Tags": "white_check_mark,moneybag" if is_win else "x,warning",
            "X-Markdown": "yes",
            "Click": "http://127.0.0.1:8005"
        }
        return self._safe_post(NTFY_URL, msg, headers)

    def send_test_ping(self) -> bool:
        """Sends an immediate verification test bulletin to Finplus-Delta."""
        lines = [
            "⚡ [DELTA RADAR TEST] Push Notifications Operational",
            "═" * 40,
            "• Channel: https://ntfy.sh/Finplus-Delta",
            "• Engine: FinPlus+ Crypto Delta Radar (APP-06)",
            "• Real-time alerts for Delta Buys, Trailing SL & Targets active.",
            "═" * 40,
            f"Time: {get_ist_now().strftime('%I:%M:%S %p IST')}"
        ]
        msg = "\n".join(lines)
        headers = {
            "Title": "⚡ FinPlus+ Delta Notifications Online",
            "Priority": "high",
            "Tags": "white_check_mark,bell,zap",
            "X-Markdown": "yes",
            "Click": "http://127.0.0.1:8005"
        }
        return self._safe_post(NTFY_URL, msg, headers)
