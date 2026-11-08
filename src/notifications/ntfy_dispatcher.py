"""
FinPlus+ NTFY Alert Dispatcher
Enforces strict 30-minute periodic pulse to eliminate overtrading.
Transmits explicit Call (CE) vs Put (PE) strike setups, and explicit
Intraday Cash BUY / SELL directives.
"""

import os
import time
import requests
import json
from typing import Dict, Any, List, Optional
from src.utils.time_and_logger import setup_logger, get_utc_iso, get_ist_time_str

logger = setup_logger("NTFYDispatcher")

DEFAULT_TOPICS = ["Finplus-Delta"]

class NTFYDispatcher:
    def __init__(self, topics: Optional[List[str]] = None, min_interval_seconds: int = 1800):
        # Prefer environment variable NTFY_TOPIC if provided
        env_topic = os.getenv("NTFY_TOPIC")
        if env_topic:
            self.topics = [env_topic.strip()]
        elif topics:
            self.topics = topics
        else:
            self.topics = DEFAULT_TOPICS
            
        self.min_interval = min_interval_seconds
        self.last_pulse_timestamp = 0.0

    def send_30m_shortlist_pulse(self, shortlist: Dict[str, Any], force: bool = False) -> bool:
        # Strict Weekend Silence (Zero notifications on Saturday & Sunday)
        from datetime import datetime, timezone, timedelta
        ist_now = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
        if not force and ist_now.weekday() >= 5: # 5 = Saturday, 6 = Sunday
            logger.info("Weekend detected (Saturday/Sunday). Skipping NTFY pulse.")
            return False

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
        cockpit_url = os.getenv("RENDER_EXTERNAL_URL", "https://finplus-stock-scanner.onrender.com" if os.getenv("FINPLUS_ENVIRONMENT") == "production" else "http://127.0.0.1:8001")
        
        for idx, topic in enumerate(self.topics):
            if idx > 0:
                time.sleep(1.5)  # Pace multi-topic requests
                
            url = f"https://ntfy.sh/{topic}"
            headers = {
                "Title": title,
                "Priority": "4",
                "Tags": "chart_with_upwards_trend,dart,moneybag",
                "Actions": f"view, Open Scanner Cockpit, {cockpit_url}"
            }
            
            # Send with single retry on 429
            for attempt in range(2):
                try:
                    resp = requests.post(url, data=message.encode('utf-8'), headers=headers, timeout=10)
                    if resp.status_code == 200:
                        logger.info(f"30m pulse delivered to '{topic}'")
                        success = True
                        break
                    elif resp.status_code == 429 and attempt == 0:
                        logger.warning(f"ntfy.sh rate limit (429) on '{topic}'. Waiting 2s before retry...")
                        time.sleep(2.0)
                        continue
                    else:
                        logger.error(f"Failed delivering pulse to '{topic}': HTTP {resp.status_code}")
                        break
                except Exception as e:
                    logger.error(f"Failed delivering pulse to '{topic}': {e}")
                    break
                
        return success

    def send_morning_tactical_briefing(self, shortlist: Optional[Dict[str, Any]] = None, force: bool = False) -> bool:
        """
        Dispatches the 09:00 AM IST Daily Tactical Morning Briefing.
        Eliminates ledger lookup by proactively declaring which segments are tradable today,
        incorporating live market shortlist suggestions and dynamic wallet milestones.
        """
        from datetime import datetime, timezone, timedelta
        ist_now = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
        day_name = ist_now.strftime("%A")  # Monday, Tuesday, etc.
        date_str = ist_now.strftime("%d-%b-%Y")
        
        # Strict Weekend Silence (No briefings on Saturday or Sunday)
        if not force and ist_now.weekday() >= 5:
            logger.info("Weekend detected (Saturday/Sunday). Skipping 09:00 AM Morning Briefing.")
            return False
        
        # Calculate Paper Crucible countdown (until Dec 1, 2026)
        crucible_end = datetime(2026, 12, 1, 23, 59, 59)
        crucible_remaining = max(0, (crucible_end.date() - ist_now.date()).days)
        is_crucible = crucible_remaining > 0
        
        is_tue_or_thu = day_name in ("Tuesday", "Thursday")
        
        lines = [
            f"🌅 **FINPLUS+ 09:00 AM TACTICAL BRIEFING** | `{day_name}, {date_str}`",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        ]
        
        if is_crucible:
            lines.append(f"⏳ **PHASE 1: PAPER TRADING CRUCIBLE**")
            lines.append(f"• **Remaining:** `{crucible_remaining} Days` (Live Accumulator starts Dec 2, 2026)")
            lines.append("• Mode: 100% Simulated fills with real INDmoney fee deductions.")
            lines.append("")
        else:
            lines.append(f"🚀 **PHASE 2: LIVE FLYWHEEL LAUNCH**")
            lines.append("• Active Capital: ₹7,750 Intraday Turbine + 5 Segregated Wallets.")
            lines.append("")
            
        lines.append("📋 **TODAY'S ACTIONABLE TRADING AGENDA:**")
        
        # 1. Intraday Core (Every Day)
        lines.append("⚡ **1. INTRADAY CORE (750 Broad Market):** `ACTIVE TODAY`")
        lines.append("• **Focus:** Single #1 Crown Hero breakout (Conviction score ≥ 80/100).")
        lines.append("• **Capital:** Deploy full ₹7,750 pool with 5x MIS leverage (~₹38,750 buying power).")
        lines.append("• **Rule:** If no stock scores ≥ 80, system orders HOLD CASH.")
        if shortlist:
            best_cash = shortlist.get("best_intraday_stock", {})
            if best_cash and best_cash.get("symbol") and best_cash.get("symbol") != "NONE":
                score = best_cash.get("confluence_score", 0)
                status_txt = "🔥 QUALIFIED" if score >= 80 else "⚠️ WATCH ONLY (Score < 80)"
                lines.append(f"• **Top Candidate:** `{best_cash.get('symbol')}` | Score: `{score}/100` ({status_txt})")
            else:
                lines.append("• **Status:** Scanning 750 broad market candidates at 09:15 open.")
        lines.append("")
        
        # 2. Sensex Asymmetric (Tuesdays & Thursdays only)
        lines.append(f"👑 **2. SENSEX 30 ASYMMETRIC TICKET (Lot Size 20):**")
        if is_tue_or_thu:
            lines.append(f"• **Status:** 🔥 `ACTIVE TODAY ({day_name.upper()})`")
            lines.append("• **Budget:** Outlay strictly < ₹500.")
            lines.append("• **Stop Loss:** Hard 75% max-loss cut.")
            lines.append("• **Gate:** Only enter if App #1 CTI confirms trend (Score ≥ +40 or ≤ -40).")
            if shortlist:
                sx = shortlist.get("sensex_radar", {})
                if sx and sx.get("symbol"):
                    lines.append(f"• **Radar Strike:** `{sx.get('symbol')}` @ ~₹{sx.get('entry_price', 0):.2f} (App #1: {sx.get('regime_badge', 'N/A')})")
        else:
            lines.append(f"• **Status:** ⏸️ `STANDBY` (Active only on Tuesdays & Thursdays).")
        lines.append("")
        
        # 3. Midcap Nifty Swing (Tuesdays & Thursdays only)
        lines.append(f"🎯 **3. MIDCAP NIFTY SWING (Twice a Week):**")
        if is_tue_or_thu:
            lines.append(f"• **Status:** 🔥 `ACTIVE TODAY ({day_name.upper()})`")
            lines.append("• **Budget:** Max ₹750 invested (1 Lot).")
            lines.append("• **Hold Plan:** Hold till 3 days before expiry.")
            lines.append("• **Safety Stop:** Hard 60% loss cut (preserves capital if momentum stalls).")
        else:
            lines.append(f"• **Status:** ⏸️ `STANDBY` (Active only on Tuesdays & Thursdays).")
        lines.append("")
        
        # 4. Nifty 50 Benchmark Setup
        if shortlist:
            nf = shortlist.get("nifty_radar", {})
            if nf and nf.get("symbol"):
                lines.append("📈 **4. NIFTY 50 RADAR SETUP:**")
                lines.append(f"• **Candidate:** `{nf.get('symbol')}` @ ~₹{nf.get('entry_price', 0):.2f} (SL: ~₹{nf.get('stop_loss', 0):.2f})")
                lines.append(f"• **Confluence:** {nf.get('regime_badge', 'N/A')}")
                lines.append("")

        # 5. MCX Commodities (Evening Session)
        lines.append("🛢️ **5. MCX COMMODITIES (Crude Mini / NatGas Mini / Gold Petal):**")
        lines.append("• **Session Window:** 17:00 – 23:30 IST (Prime US Window: 18:00–21:30).")
        lines.append("• **Priority Engine:** App #3 signals Crude Mini (10 Bbl), NatGas Mini (250 mmBtu), Gold Petal (1g).")
        if day_name == "Wednesday":
            lines.append("• ⚠️ **EIA Alert:** US Crude Oil Inventories release tonight at 20:00 IST! Shield active.")
        elif day_name == "Thursday":
            lines.append("• ⚠️ **EIA Alert:** US Natural Gas Storage report release tonight at 20:00 IST! Shield active.")
        lines.append("")
        
        # 6. Dynamic Live Wallet Milestones (queried from App #4 Journal)
        try:
            resp = requests.get("http://127.0.0.1:8003/api/wallets", timeout=1.5)
            if resp.status_code == 200:
                w_data = resp.json().get("wallets", [])
                unlocked = [w for w in w_data if w.get("is_unlocked")]
                accumulating = [w for w in w_data if not w.get("is_unlocked")]
                lines.append("🏦 **LIVE WALLET MILESTONES & CAPITAL STATUS:**")
                if unlocked:
                    for uw in unlocked:
                        lines.append(f"• 🎉 **{uw['display_name']}**: `UNLOCKED` (₹{uw['balance']:,.2f} ≥ ₹{uw['target_threshold']:,.2f}) - Ready to trade!")
                else:
                    lines.append("• All 5 Target Wallets actively accumulating via ₹250/day micro-savings.")
                for aw in accumulating[:3]:
                    short_fall = round(aw['target_threshold'] - aw['balance'], 2)
                    lines.append(f"• {aw['display_name']}: ₹{aw['balance']:,.2f} / ₹{aw['target_threshold']:,.2f} (`₹{short_fall:,.2f} left`)")
                lines.append("")
        except Exception:
            pass

        # 7. 4-Hour Timeframe Checks
        lines.append("💎 **6. 4-HOUR PENNY (< ₹50, Zero Debt) & LONG-TERM:**")
        lines.append("• Check 4H closed candles at `13:15 IST` and `15:30 IST` for Stage 2 breakouts.")
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        lines.append("🛡️ *FinPlus+ Institutional Discipline Protocol.*")

        message = "\n".join(lines)
        title = f"FinPlus+ 09:00 AM Agenda: What to Trade Today ({day_name})"
        
        cockpit_url = os.getenv("RENDER_EXTERNAL_URL", "https://finplus-stock-scanner.onrender.com" if os.getenv("FINPLUS_ENVIRONMENT") == "production" else "http://127.0.0.1:8001")
        
        success = False
        for topic in self.topics:
            url = f"https://ntfy.sh/{topic}"
            headers = {
                "Title": title,
                "Priority": "5",
                "Tags": "sunrise,calendar,alarm_clock,dart",
                "Actions": f"view, Open Scanner Cockpit, {cockpit_url}"
            }
            try:
                resp = requests.post(url, data=message.encode('utf-8'), headers=headers, timeout=10)
                if resp.status_code == 200:
                    logger.info(f"09:00 AM Tactical Briefing delivered to '{topic}'")
                    success = True
            except Exception as e:
                logger.error(f"Failed delivering morning briefing to '{topic}': {e}")
                
        return success
