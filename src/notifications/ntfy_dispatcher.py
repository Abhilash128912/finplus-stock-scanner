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

    def _fetch_app1_regime(self) -> Dict[str, Any]:
        """Queries App #1 Macro Trend & Air Traffic Control locally or in cloud."""
        try:
            app1_url = os.getenv("APP1_URL", "http://127.0.0.1:8000")
            resp = requests.get(f"{app1_url}/api/regime", timeout=1.5)
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            pass
        return {}

    def send_30m_shortlist_pulse(self, shortlist: Dict[str, Any], force: bool = False) -> bool:
        """
        Dispatches the 30-minute consolidated trade pulse during market hours (09:15 to 15:00 IST).
        Strictly silent on weekends and outside 09:15 - 15:00 IST (stops 30 mins before 15:30 close).
        Contains in exact numbered sequence:
        1) Market Trend Status (App #1 Breadth Gate)
        2) Best Intraday Trade (750 Broad Market #1 Crown Hero >= 80/100 or HOLD CASH)
        3) Best Sensex Option (Active reminders on Wednesday & Thursday)
        4) Best Nifty Option
        5) Best Stock Option (F&O Lot <= 500 + Reliance)
        + Midcap Nifty Swing (Active reminders on Monday & Tuesday)
        """
        from datetime import datetime, timezone, timedelta, time as dt_time
        ist_now = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
        
        # Strict Weekend Silence (Zero notifications on Saturday & Sunday)
        if not force and ist_now.weekday() >= 5: # 5 = Saturday, 6 = Sunday
            logger.info("Weekend detected (Saturday/Sunday). Skipping NTFY pulse.")
            return False

        # Pulse Window: 09:15 to 15:00 IST (stops 30 mins before 15:30 close)
        curr_time = ist_now.time()
        if not force and not (dt_time(9, 15) <= curr_time <= dt_time(15, 0)):
            logger.info(f"Outside 30m pulse window (09:15 - 15:00 IST, current: {curr_time}). Skipping pulse.")
            return False

        now = time.time()
        if not force and (now - self.last_pulse_timestamp) < self.min_interval:
            logger.info("Skipping NTFY pulse: 30m interval not reached yet.")
            return False
            
        self.last_pulse_timestamp = now
        ist_time = shortlist.get("ist_time", get_ist_time_str())
        day_name = ist_now.strftime("%A")
        is_sensex_day = day_name in ("Wednesday", "Thursday")
        is_midcap_day = day_name in ("Monday", "Tuesday")
        
        sx = shortlist.get("sensex_radar", {})
        nf = shortlist.get("nifty_radar", {})
        mid = shortlist.get("midcap_radar", {})
        stk = shortlist.get("best_stock_option", {})
        cash = shortlist.get("best_intraday_stock", {})
        
        # 1. Market Trend Status (App #1 Breadth Gate)
        app1_data = self._fetch_app1_regime()
        trend_info = app1_data.get("trend", {})
        opt_dir = app1_data.get("options_directive", {})
        metrics = app1_data.get("metrics", {})
        
        lines = [
            f"🏛️ **FINPLUS+ 30-MIN INTRADAY PULSE** | `{day_name}, {ist_time}`",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        ]
        
        # 1) MARKET TREND STATUS
        lines.append("🌐 **1. MARKET TREND STATUS (App #1 Breadth Gate):**")
        if trend_info:
            score_lbl = trend_info.get("score_label", f"{trend_info.get('score', 0)}/100")
            lines.append(f"• **CTI Trend:** `{trend_info.get('label', 'N/A')}` ({score_lbl})")
            lines.append(f"• **NSE Breadth:** `{metrics.get('broad_advances', '--')} Adv` / `{metrics.get('broad_declines', '--')} Dec` (2,266 Equities, ADR: `{metrics.get('broad_adr', '--')}`)")
            lines.append(f"• **Air Traffic Veto:** `{opt_dir.get('badge', 'ACTIVE')}`")
        else:
            diag = shortlist.get("market_diagnostic", {})
            lines.append(f"• **CTI Score:** `{diag.get('trend_score', 0)}/100` | `{diag.get('trend_label', 'ACTIVE')}`")
            lines.append(f"• **Gate Clearance:** `{diag.get('status', 'ACTIVE')}`")
        lines.append("")
        
        # 2) BEST INTRADAY TRADE (750 Broad Market Quality)
        lines.append("⚡ **2. SPECIFIC INTRADAY EQUITY SUGGESTIONS (750 Broad Market):**")
        top_stocks = shortlist.get("top_intraday_stocks", [])
        if not top_stocks and shortlist.get("best_intraday_stock"):
            top_stocks = [shortlist.get("best_intraday_stock")]

        if top_stocks:
            for idx, stk in enumerate(top_stocks, 1):
                sym = stk.get("symbol")
                ltp = stk.get("last_price")
                act = stk.get("intraday_action_badge", "BUY")
                score = stk.get("confluence_score", 0)
                risk = stk.get("risk_management", {})
                sl = risk.get("stop_loss", stk.get("range_bottom", "--"))
                tgt = risk.get("target", "--")
                lines.append(f"• **#{idx} {act}: `{sym}` @ `₹{ltp}`**")
                lines.append(f"  🎯 **Target:** `₹{tgt}` | 🛑 **SL:** `₹{sl}` | Score: `{score}/100`")
                lines.append(f"  📊 **50 EMA:** `₹{stk.get('ema50')}` ({stk.get('ema50_dist_pct', 0):+0.2f}%) | Anti-Circuit: `{stk.get('anti_circuit_status', 'SAFE')}`")
        else:
            top_w = shortlist.get("top_watch_cash")
            watch_list = shortlist.get("top_watch_stocks", [])
            if watch_list:
                lines.append("• ⚠️ **NO HEROS CLEARED 80/100 THRESHOLD (HOLD CASH RULE)**")
                lines.append("• **Watched Equities (Awaiting Breakout Confirmation):**")
                for idx, stk in enumerate(watch_list[:2], 1):
                    sym = stk.get("symbol")
                    ltp = stk.get("last_price")
                    trigger = stk.get("range_top") if stk.get("signal_direction", 1) >= 0 else stk.get("range_bottom")
                    lines.append(f"  {idx}. `{sym}` @ ₹{ltp} — Needs 15m candle close above ₹{trigger} (Score: {stk.get('confluence_score')}/100)")
            else:
                lines.append("• **Recommendation:** `HOLD CASH (All 750 scrips inside range bands)`")
        lines.append("")

        # 3) BEST SENSEX OPTION (Active on Wednesday & Thursday)
        lines.append("👑 **3. BEST SENSEX 30 OPTION:**")
        if is_sensex_day:
            lines.append(f"• **Status:** 🔥 `ACTIVE TODAY ({day_name.upper()})`")
            lines.append("• 👉 **REMINDER: Take Sensex Asymmetric entry (Lot 20, Outlay < ₹500, 75% Stop Loss).**")
            if sx.get("is_actionable") and sx.get("recommended_strike"):
                lines.append(f"• **CONFIRMED STRIKE:** `{sx.get('recommended_strike')}` | **ACTION:** `{sx.get('action_badge')}`")
                lines.append(f"• **Spot LTP:** `₹{sx.get('last_price', '--')}` | **50 EMA:** `₹{sx.get('ema50', '--')}` ({sx.get('ema50_dist_pct', 0):+0.2f}%)")
                lines.append(f"• **Setup:** {sx.get('directive')}")
            else:
                lines.append(f"• **Strike Stance:** `HOLD CASH / STANDBY` ({sx.get('directive')})")
        else:
            lines.append(f"• **Status:** ⏸️ `STANDBY` (Sensex trades scheduled for Wednesday & Thursday).")
        lines.append("")

        # 4) BEST NIFTY OPTION
        lines.append("🎯 **4. BEST NIFTY 50 OPTION:**")
        lines.append(f"• **Spot LTP:** `₹{nf.get('last_price', '--')}` ({nf.get('pct_change_pdc', 0):+0.2f}%) | **50 EMA:** `₹{nf.get('ema50', '--')}` ({nf.get('ema50_dist_pct', 0):+0.2f}%)")
        if nf.get("is_actionable") and nf.get("recommended_strike"):
            lines.append(f"• **CONFIRMED STRIKE:** `{nf.get('recommended_strike')}` | **ACTION:** `{nf.get('action_badge')}`")
            lines.append(f"• **Setup:** {nf.get('directive')}")
        else:
            lines.append(f"• **Recommendation:** `HOLD CASH / STANDBY` ({nf.get('directive')})")
        lines.append("")

        # 5) BEST STOCK OPTION (Updates every 30m till 30m before close)
        lines.append("💎 **5. BEST STOCK OPTION (F&O Lot ≤ 500 + Reliance):**")
        if stk:
            lines.append(f"• **CONFIRMED CONTRACT:** `{stk.get('recommended_contract')}` (Lot: `{stk.get('lot_size')}` | Spot: `₹{stk.get('last_price')}`)")
            lines.append(f"• **ACTION:** `{stk.get('option_action_badge')}` | **Score:** `{stk.get('confluence_score')}/100`")
            lines.append(f"• **50 EMA Alignment:** `₹{stk.get('ema50')}` ({stk.get('ema50_dist_pct', 0):+0.2f}%)")
            risk = stk.get("risk_management", {})
            if "underlying_sl" in risk:
                lines.append(f"• **Underlying SL:** `₹{risk.get('underlying_sl')}` | **Target:** `₹{risk.get('underlying_target')}`")
            lines.append(f"• **Regime Clearance:** `{stk.get('regime_badge')}`")
        else:
            reason = shortlist.get("stock_option_standby_reason", "No setup cleared macro trend + 50pt confluence")
            lines.append(f"• **Recommendation:** `HOLD CASH` ({reason})")
        lines.append("")

        # 6) MIDCAP NIFTY SWING (Active on Monday & Tuesday)
        lines.append("🎯 **6. MIDCAP NIFTY SWING (Twice a Week / Expiry):**")
        if is_midcap_day:
            lines.append(f"• **Status:** 🔥 `ACTIVE TODAY ({day_name.upper()})`")
            lines.append("• 👉 **REMINDER: Take Midcap Nifty trade (Lot 75, Outlay < ₹750, 60% Stop Loss, hold till 3 days before exp).**")
            if mid.get("is_actionable") and mid.get("recommended_strike"):
                lines.append(f"• **CONFIRMED STRIKE:** `{mid.get('recommended_strike')}` | **ACTION:** `{mid.get('action_badge')}`")
                lines.append(f"• **Setup:** {mid.get('directive')}")
            else:
                lines.append(f"• **Strike Stance:** `HOLD CASH / STANDBY` ({mid.get('directive', 'Awaiting breakout confirmation')})")
        else:
            lines.append(f"• **Status:** ⏸️ `STANDBY` (Midcap trades scheduled for Monday & Tuesday).")
        lines.append("")

        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        lines.append("🛡️ *Next 30-min consolidated pulse scheduled. Intraday pulses end at 15:00 IST.*")

        message = "\n".join(lines)
        title = f"FinPlus+ 30m Trade Directives ({ist_time})"
        
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
        
        is_sensex_day = day_name in ("Wednesday", "Thursday")
        is_midcap_day = day_name in ("Monday", "Tuesday")
        
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
        lines.append("• **Focus:** High-conviction breakout equities (Conviction score ≥ 80/100).")
        lines.append("• **Capital:** Deploy full ₹7,750 pool with 5x MIS leverage (~₹38,750 buying power).")
        lines.append("• **Rule:** If no stock scores ≥ 80, system orders HOLD CASH.")
        if shortlist:
            top_stocks = shortlist.get("top_intraday_stocks", [])
            if not top_stocks and shortlist.get("best_intraday_stock"):
                top_stocks = [shortlist.get("best_intraday_stock")]
            
            if top_stocks:
                lines.append("• **Specific Intraday Equity Breakouts:**")
                for idx, stk in enumerate(top_stocks, 1):
                    sym = stk.get("symbol")
                    ltp = stk.get("last_price")
                    act = stk.get("intraday_action_badge", "BUY")
                    risk = stk.get("risk_management", {})
                    sl = risk.get("stop_loss", "--")
                    tgt = risk.get("target", "--")
                    score = stk.get("confluence_score", 0)
                    lines.append(f"  #{idx} **{act}: `{sym}` @ `₹{ltp}`** (SL: ₹{sl} | Target: ₹{tgt} | Score: {score}/100)")
            else:
                watch_stocks = shortlist.get("top_watch_stocks", [])
                if watch_stocks:
                    lines.append("• **Top Pre-Market Watch Equities (Key Levels):**")
                    for idx, stk in enumerate(watch_stocks[:3], 1):
                        lines.append(f"  #{idx} `{stk.get('symbol')}` @ ₹{stk.get('last_price')} | Trigger: Needs 15m close above ₹{stk.get('range_top')} (Score: {stk.get('confluence_score')}/100)")
                else:
                    lines.append("• **Status:** Scanning 750 broad market candidates at 09:15 open.")
        lines.append("")
        
        # 2. Midcap Nifty Swing (Mondays & Tuesdays: Before Expiry & On Expiry)
        lines.append(f"🎯 **2. MIDCAP NIFTY SWING (Lot Size 75):**")
        if is_midcap_day:
            lines.append(f"• **Status:** 🔥 `ACTIVE TODAY ({day_name.upper()})`")
            lines.append("• 👉 **REMINDER: Take Midcap Nifty entry today!**")
            lines.append("• **Budget:** Max ₹750 invested (1 Lot).")
            lines.append("• **Hold Plan:** Hold till 3 days before expiry or expiry afternoon.")
            lines.append("• **Safety Stop:** Hard 60% loss cut (preserves capital if momentum stalls).")
            if shortlist:
                mid = shortlist.get("midcap_radar", {})
                if mid and mid.get("symbol"):
                    lines.append(f"• **Radar Strike:** `{mid.get('recommended_strike', mid.get('symbol'))}` @ ~₹{mid.get('entry_price', 0):.2f}")
        else:
            lines.append(f"• **Status:** ⏸️ `STANDBY` (Active only on Mondays & Tuesdays).")
        lines.append("")

        # 3. Sensex Asymmetric (Wednesdays & Thursdays: Before Expiry & On Expiry)
        lines.append(f"👑 **3. SENSEX 30 ASYMMETRIC TICKET (Lot Size 20):**")
        if is_sensex_day:
            lines.append(f"• **Status:** 🔥 `ACTIVE TODAY ({day_name.upper()})`")
            lines.append("• 👉 **REMINDER: Take Sensex Asymmetric entry today!**")
            lines.append("• **Budget:** Outlay strictly < ₹500.")
            lines.append("• **Stop Loss:** Hard 75% max-loss cut.")
            lines.append("• **Gate:** Only enter if App #1 CTI confirms trend (Score ≥ +40 or ≤ -40).")
            if shortlist:
                sx = shortlist.get("sensex_radar", {})
                if sx and sx.get("symbol"):
                    lines.append(f"• **Radar Strike:** `{sx.get('symbol')}` @ ~₹{sx.get('entry_price', 0):.2f} (App #1: {sx.get('regime_badge', 'N/A')})")
        else:
            lines.append(f"• **Status:** ⏸️ `STANDBY` (Active only on Wednesdays & Thursdays).")
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
