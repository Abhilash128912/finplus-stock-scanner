"""
FinPlus+ NTFY Alert Dispatcher
Enforces strict 30-minute periodic pulse to eliminate overtrading.
Transmits explicit Call (CE) vs Put (PE) strike setups, and explicit
Intraday Cash BUY / SELL directives.
Uses professional institutional Markdown formatting and universal Render cloud links.
"""

import os
import time
import requests
import json
from typing import Dict, Any, List, Optional
from src.utils.time_and_logger import setup_logger, get_utc_iso, get_ist_time_str

logger = setup_logger("NTFYDispatcher")

DEFAULT_TOPICS = ["Finplus-Delta"]
RENDER_SCANNER_URL = "https://finplus-stock-scanner.onrender.com"

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

    def _fetch_app3_commodity(self) -> Dict[str, Any]:
        """Queries App #3 MCX Commodity Radar."""
        try:
            app3_url = os.getenv("APP3_URL", "http://127.0.0.1:8002")
            resp = requests.get(f"{app3_url}/api/scan", timeout=1.5)
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            pass
        return {}

    def _fetch_morning_macro_news(self) -> Dict[str, str]:
        """Generates crisp morning news brief and global cues."""
        return {
            "gift_nifty": "Consolidation bias near key swing levels",
            "global_cues": "US markets steady, Asian equities mixed with selective rotation",
            "macro_sentiment": "Institutional FII/DII focus on earnings reactions & volume breakouts"
        }

    def send_30m_shortlist_pulse(self, shortlist: Dict[str, Any], force: bool = False) -> bool:
        """
        Dispatches the 30-minute consolidated trade pulse during market hours (09:15 to 15:00 IST).
        Strictly silent on weekends and outside 09:15 - 15:00 IST (stops 30 mins before 15:30 close).
        Formatted in institutional, clean, executive Markdown for high readability.
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
        
        # 1. Market Trend Status (App #1 Breadth Gate)
        app1_data = self._fetch_app1_regime()
        trend_info = app1_data.get("trend", {})
        opt_dir = app1_data.get("options_directive", {})
        metrics = app1_data.get("metrics", {})
        
        lines = [
            f"## 🏛️ FINPLUS+ CONSOLIDATED PULSE",
            f"**{day_name} • {ist_time}**",
            ""
        ]
        
        # 1) MARKET TREND STATUS
        lines.append("### 🌐 1. MARKET REGIME & BREADTH")
        if trend_info:
            score_lbl = trend_info.get("score_label", f"{trend_info.get('score', 0)}/100")
            lines.append(f"• **Trend:** `{trend_info.get('label', 'N/A')}` ({score_lbl}) | **Gate:** `{opt_dir.get('badge', 'ACTIVE')}`")
            lines.append(f"• **NSE Breadth:** `{metrics.get('broad_advances', '--')} Adv` / `{metrics.get('broad_declines', '--')} Dec` (ADR: `{metrics.get('broad_adr', '--')}`)")
        else:
            diag = shortlist.get("market_diagnostic", {})
            lines.append(f"• **Trend Score:** `{diag.get('trend_score', 0)}/100` | `{diag.get('trend_label', 'ACTIVE')}`")
            lines.append(f"• **Gate Clearance:** `{diag.get('status', 'ACTIVE')}`")
        lines.append("")
        
        # 2) BEST INTRADAY TRADE (750 Broad Market Quality)
        lines.append("### ⚡ 2. INTRADAY EQUITY SUGGESTIONS (750 Broad Market)")
        top_stocks = shortlist.get("top_intraday_stocks", [])
        if not top_stocks and shortlist.get("best_intraday_stock"):
            top_stocks = [shortlist.get("best_intraday_stock")]

        if top_stocks:
            for idx, item in enumerate(top_stocks, 1):
                sym = item.get("symbol")
                ltp = item.get("last_price")
                act = item.get("intraday_action_badge", "BUY")
                act_icon = "🟢" if "BUY" in act else "🔴"
                score = item.get("confluence_score", 0)
                risk = item.get("risk_management", {})
                sl = risk.get("stop_loss", item.get("range_bottom", "--"))
                tgt = risk.get("target", "--")
                lines.append(f"• **#{idx} {act_icon} {act}: `{sym}` @ ₹{ltp}** (Score: `{score}/100`)")
                lines.append(f"  🎯 Target: `₹{tgt}` | 🛑 SL: `₹{sl}`")
                lines.append(f"  📊 50 EMA: `₹{item.get('ema50')}` ({item.get('ema50_dist_pct', 0):+0.2f}%) | Anti-Circuit: `{item.get('anti_circuit_status', 'SAFE')}`")
        else:
            watch_list = shortlist.get("top_watch_stocks", [])
            if watch_list:
                lines.append("• ⏸️ **HOLD CASH** (No broad-market scrip cleared 80/100 breakout threshold)")
                lines.append("• **Key Levels Watched:**")
                for idx, item in enumerate(watch_list[:2], 1):
                    sym = item.get("symbol")
                    ltp = item.get("last_price")
                    trigger = item.get("range_top") if item.get("signal_direction", 1) >= 0 else item.get("range_bottom")
                    lines.append(f"  {idx}. `{sym}` @ ₹{ltp} — Needs 15m candle close past ₹{trigger} (Score: {item.get('confluence_score')}/100)")
            else:
                lines.append("• ⏸️ **Recommendation:** `HOLD CASH (All 750 scrips inside range bands)`")
        lines.append("")

        # 3) BEST SENSEX OPTION (Active on Wednesday & Thursday)
        lines.append("### 👑 3. SENSEX 30 OPTION")
        if is_sensex_day:
            lines.append(f"• **Status:** 🔥 `ACTIVE TODAY ({day_name.upper()})`")
            lines.append("• 💡 **Protocol:** Outlay < ₹500 (Lot 20), Hard 75% Stop Loss.")
            if sx.get("is_actionable") and sx.get("recommended_strike"):
                lines.append(f"• **Strike:** `{sx.get('recommended_strike')}` | **Action:** `{sx.get('action_badge')}`")
                lines.append(f"• **Spot:** `₹{sx.get('last_price', '--')}` | **50 EMA:** `₹{sx.get('ema50', '--')}` ({sx.get('ema50_dist_pct', 0):+0.2f}%)")
                lines.append(f"• **Setup:** {sx.get('directive')}")
            else:
                lines.append(f"• **Stance:** `HOLD CASH / STANDBY` ({sx.get('directive')})")
        else:
            lines.append(f"• **Status:** ⏸️ `STANDBY` (Sensex scheduled for Wednesday & Thursday).")
        lines.append("")

        # 4) BEST NIFTY OPTION
        lines.append("### 🎯 4. NIFTY 50 BENCHMARK")
        lines.append(f"• **Spot:** `₹{nf.get('last_price', '--')}` ({nf.get('pct_change_pdc', 0):+0.2f}%) | **50 EMA:** `₹{nf.get('ema50', '--')}` ({nf.get('ema50_dist_pct', 0):+0.2f}%)")
        if nf.get("is_actionable") and nf.get("recommended_strike"):
            lines.append(f"• **Strike:** `{nf.get('recommended_strike')}` | **Action:** `{nf.get('action_badge')}`")
            lines.append(f"• **Setup:** {nf.get('directive')}")
        else:
            lines.append(f"• **Recommendation:** `HOLD CASH / STANDBY` ({nf.get('directive')})")
        lines.append("")

        # 5) BEST STOCK OPTION (Updates every 30m till 30m before close)
        lines.append("### 💎 5. STOCK OPTION (F&O Lot ≤ 500)")
        if stk:
            lines.append(f"• **Contract:** `{stk.get('recommended_contract')}` (Lot: `{stk.get('lot_size')}` | Spot: `₹{stk.get('last_price')}`)")
            lines.append(f"• **Action:** `{stk.get('option_action_badge')}` | **Score:** `{stk.get('confluence_score')}/100`")
            risk = stk.get("risk_management", {})
            if "underlying_sl" in risk:
                lines.append(f"• **Target:** `₹{risk.get('underlying_target')}` | **SL:** `₹{risk.get('underlying_sl')}`")
            lines.append(f"• **Regime Clearance:** `{stk.get('regime_badge')}`")
        else:
            reason = shortlist.get("stock_option_standby_reason", "No setup cleared macro trend + 50pt confluence")
            lines.append(f"• **Recommendation:** `HOLD CASH` ({reason})")
        lines.append("")

        # 6) MIDCAP NIFTY SWING (Active on Monday & Tuesday)
        lines.append("### 🎯 6. MIDCAP NIFTY SWING")
        if is_midcap_day:
            lines.append(f"• **Status:** 🔥 `ACTIVE TODAY ({day_name.upper()})`")
            lines.append("• 💡 **Protocol:** Outlay < ₹750 (Lot 75), Hard 60% Stop Loss, hold till pre-expiry/expiry.")
            if mid.get("is_actionable") and mid.get("recommended_strike"):
                lines.append(f"• **Strike:** `{mid.get('recommended_strike')}` | **Action:** `{mid.get('action_badge')}`")
                lines.append(f"• **Setup:** {mid.get('directive')}")
            else:
                lines.append(f"• **Stance:** `HOLD CASH / STANDBY` ({mid.get('directive', 'Awaiting breakout')})")
        else:
            lines.append(f"• **Status:** ⏸️ `STANDBY` (Midcap scheduled for Monday & Tuesday).")
        lines.append("")

        # 7) MCX COMMODITIES (Crude Mini, NatGas Mini, Gold Petal)
        app3_data = self._fetch_app3_commodity()
        lines.append("### 🛢️ 7. MCX COMMODITY RADAR (Evening Prime 18:00 - 21:30)")
        if app3_data:
            p = app3_data.get("priority_trade", {})
            comms = app3_data.get("commodities", {})
            crude = comms.get("CRUDEOILM", {})
            natgas = comms.get("NATGASMINI", {})
            gold = comms.get("GOLDPETAL", {})
            
            lines.append(f"• **LTP:** Crude: `₹{crude.get('ltp', 0):,.0f}` | NatGas: `₹{natgas.get('ltp', 0):,.1f}` | Gold: `₹{gold.get('ltp', 0):,.0f}`")
            if p and p.get("symbol"):
                lines.append(f"• **Priority Asset:** `{p.get('display_name')}` ({p.get('futures_directive')})")
                lines.append(f"• **Status:** {p.get('reason', 'Awaiting breakout')} | Score: `{p.get('confluence_score', 0)}/100`")
        else:
            lines.append("• **Status:** Tracking Crude Mini, NatGas Mini, and Gold Petal for 18:00 US session open.")
        lines.append("")

        lines.append("---")
        lines.append(f"🛡️ *FinPlus+ Institutional Pulse • Pulses cease at 15:00 IST*")
        lines.append(f"🌐 [Open Cloud Scanner Cockpit]({RENDER_SCANNER_URL})")

        message = "\n".join(lines)
        title = f"FinPlus+ 30m Directives ({ist_time})"
        
        success = False
        cockpit_url = RENDER_SCANNER_URL
        
        for idx, topic in enumerate(self.topics):
            if idx > 0:
                time.sleep(1.5)  # Pace multi-topic requests
                
            url = f"https://ntfy.sh/{topic}"
            headers = {
                "Title": title,
                "Priority": "4",
                "Tags": "chart_with_upwards_trend,dart,moneybag",
                "Markdown": "yes",
                "X-Markdown": "yes",
                "Click": cockpit_url,
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
        Features Morning News Brief, MCX EIA Economic Calendar Timings & Inventories,
        Actionable Segment Schedule, Specific Equity Breakouts, and Live Dynamic Wallet Milestones.
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
        
        news = self._fetch_morning_macro_news()
        
        lines = [
            f"## 🌅 FINPLUS+ 09:00 AM TACTICAL BRIEFING",
            f"**{day_name}, {date_str}**",
            ""
        ]
        
        # Phase Banner
        if is_crucible:
            lines.append(f"⏳ **PHASE 1: PAPER CRUCIBLE ({crucible_remaining} Days Remaining)**")
            lines.append("• Mode: 100% Simulated execution with real INDmoney fee deductions.")
        else:
            lines.append("🚀 **PHASE 2: LIVE FLYWHEEL ACTIVE**")
            lines.append("• Capital: ₹7,750 Intraday Turbine + 5 Segregated Wallets.")
        lines.append("")

        # 1. MORNING NEWS BRIEF & GLOBAL CUES
        lines.append("### 📰 1. MORNING NEWS BRIEF & GLOBAL CUES")
        lines.append(f"• **Gift Nifty:** {news['gift_nifty']}")
        lines.append(f"• **Global Markets:** {news['global_cues']}")
        lines.append(f"• **Macro Bias:** {news['macro_sentiment']}")
        lines.append("")

        # 2. TODAY'S ACTIONABLE TRADING AGENDA
        lines.append("### 📋 2. TODAY'S SEGMENT DIRECTIVE")
        
        # Midcap Nifty Swing
        if is_midcap_day:
            lines.append(f"• 🎯 **MIDCAP NIFTY SWING:** 🔥 **ACTIVE TODAY ({day_name.upper()})**")
            lines.append("  👉 Plan: Take 1 Lot (75 qty), Outlay < ₹750, 60% SL, hold till expiry/pre-expiry.")
        else:
            lines.append("• 🎯 **MIDCAP NIFTY SWING:** ⏸️ `STANDBY` (Active only on Mondays & Tuesdays)")

        # Sensex 30 Asymmetric
        if is_sensex_day:
            lines.append(f"• 👑 **SENSEX 30 ASYMMETRIC:** 🔥 **ACTIVE TODAY ({day_name.upper()})**")
            lines.append("  👉 Plan: Outlay < ₹500 (Lot 20), Hard 75% SL, enter only on confirmed App #1 CTI.")
        else:
            lines.append("• 👑 **SENSEX 30 ASYMMETRIC:** ⏸️ `STANDBY` (Active only on Wednesdays & Thursdays)")

        # Intraday Core (Every day)
        lines.append("• ⚡ **INTRADAY CORE (750 Equities):** 🔥 **ACTIVE TODAY** (09:15 - 15:00 IST)")
        lines.append("  👉 Plan: Deploy ₹7,750 turbine pool with 5x MIS leverage. Minimum 80/100 conviction or HOLD CASH.")
        lines.append("")

        # 3. SPECIFIC INTRADAY STOCKS & PRE-MARKET RADAR
        lines.append("### ⚡ 3. SPECIFIC EQUITY SUGGESTIONS")
        if shortlist:
            top_stocks = shortlist.get("top_intraday_stocks", [])
            if not top_stocks and shortlist.get("best_intraday_stock"):
                top_stocks = [shortlist.get("best_intraday_stock")]
            
            if top_stocks:
                for idx, item in enumerate(top_stocks, 1):
                    sym = item.get("symbol")
                    ltp = item.get("last_price")
                    act = item.get("intraday_action_badge", "BUY")
                    act_icon = "🟢" if "BUY" in act else "🔴"
                    risk = item.get("risk_management", {})
                    sl = risk.get("stop_loss", "--")
                    tgt = risk.get("target", "--")
                    score = item.get("confluence_score", 0)
                    lines.append(f"• **#{idx} {act_icon} {act}: `{sym}` @ ₹{ltp}** (Score: `{score}/100`)")
                    lines.append(f"  🎯 Target: `₹{tgt}` | 🛑 SL: `₹{sl}`")
            else:
                watch_stocks = shortlist.get("top_watch_stocks", [])
                if watch_stocks:
                    lines.append("• **Top Pre-Market Key Levels (15m Breakout Watch):**")
                    for idx, item in enumerate(watch_stocks[:3], 1):
                        lines.append(f"  #{idx} `{item.get('symbol')}` @ ₹{item.get('last_price')} | Trigger: 15m close above ₹{item.get('range_top')} (Score: {item.get('confluence_score')}/100)")
                else:
                    lines.append("• **Scanning 750 broad market candidates at 09:15 market open.**")
        else:
            lines.append("• **Scanning broad market candidates at 09:15 open.**")
        lines.append("")

        # 4. MCX COMMODITIES & EIA ECONOMIC CALENDAR TIMING
        lines.append("### 🛢️ 4. MCX COMMODITIES & EIA CALENDAR")
        lines.append("• **Trading Session:** 17:00 – 23:30 IST (Prime US Window: 18:00 – 21:30 IST)")
        lines.append("• **Instruments:** Crude Mini (10 Bbl), NatGas Mini (250 mmBtu), Gold Petal (1g)")
        
        if day_name == "Wednesday":
            lines.append("• ⚠️ **EIA ALERT TODAY:** **US Crude Oil Inventories** at **20:00 / 20:30 IST**!")
            lines.append("  👉 Volatility Shield: Flat across release. No new Crude entries 15 mins before data.")
        elif day_name == "Thursday":
            lines.append("• ⚠️ **EIA ALERT TODAY:** **US Natural Gas Storage Report** at **20:00 / 20:30 IST**!")
            lines.append("  👉 Volatility Shield: Flat across release. No new NatGas entries 15 mins before data.")
        else:
            lines.append("• **Macro Calendar:** Standard trend-following. EIA reports scheduled for Wed (Crude) & Thu (NatGas).")
        lines.append("")

        # 5. DYNAMIC LIVE WALLET MILESTONES (App #4 Journal)
        try:
            resp = requests.get("http://127.0.0.1:8003/api/wallets", timeout=1.5)
            if resp.status_code == 200:
                w_data = resp.json().get("wallets", [])
                unlocked = [w for w in w_data if w.get("is_unlocked")]
                accumulating = [w for w in w_data if not w.get("is_unlocked")]
                lines.append("### 🏦 5. LIVE WALLET MILESTONES & CAPITAL")
                if unlocked:
                    for uw in unlocked:
                        lines.append(f"• 🎉 **{uw['display_name']}**: `UNLOCKED` (₹{uw['balance']:,.2f} ≥ ₹{uw['target_threshold']:,.2f}) — Ready to trade!")
                else:
                    lines.append("• Target Wallets actively accumulating via ₹250/day micro-savings + cross-P&L.")
                for aw in accumulating[:3]:
                    short_fall = round(aw['target_threshold'] - aw['balance'], 2)
                    lines.append(f"• **{aw['display_name']}**: ₹{aw['balance']:,.2f} / ₹{aw['target_threshold']:,.2f} (`₹{short_fall:,.2f} left`)")
                lines.append("")
        except Exception:
            pass

        # 6. 4-Hour Timeframe Checks
        lines.append("### 💎 6. 4-HOUR TIMEFRAME CHECKPOINTS")
        lines.append("• Inspect 4H closed candles at `13:15 IST` and `15:30 IST` for Penny (< ₹50, Zero Debt) breakouts.")
        lines.append("")
        lines.append("---")
        lines.append(f"🛡️ *FinPlus+ Institutional Discipline Protocol.*")
        lines.append(f"🌐 [Open Cloud Scanner Cockpit]({RENDER_SCANNER_URL})")

        message = "\n".join(lines)
        title = f"FinPlus+ 09:00 AM Agenda ({day_name})"
        
        cockpit_url = RENDER_SCANNER_URL
        success = False
        
        for topic in self.topics:
            url = f"https://ntfy.sh/{topic}"
            headers = {
                "Title": title,
                "Priority": "5",
                "Tags": "sunrise,calendar,alarm_clock,dart",
                "Markdown": "yes",
                "X-Markdown": "yes",
                "Click": cockpit_url,
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
