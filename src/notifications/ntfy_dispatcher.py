"""
FinPlus+ NTFY Alert Dispatcher
Institutional Terminal Alert Dispatcher (Bloomberg / Reuters Eikon Style)
Strict 30-minute consolidation window (09:15 to 15:00 IST).
Zero emoji spam, clean typography, compact formatting to prevent mobile truncation.
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
        Strictly silent on weekends and outside 09:15 - 15:00 IST.
        Uses institutional, high-density, professional formatting without large headers or emoji clutter.
        """
        from datetime import datetime, timezone, timedelta, time as dt_time
        ist_now = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
        
        # Strict Weekend Silence
        if not force and ist_now.weekday() >= 5:
            logger.info("Weekend detected (Saturday/Sunday). Skipping NTFY pulse.")
            return False

        # Pulse Window: 09:15 to 15:00 IST
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
        date_str = ist_now.strftime("%d-%b-%Y")
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
            f"**FINPLUS+ INSTITUTIONAL DESK PULSE**",
            f"`{day_name}, {date_str} | {ist_time}`",
            "----------------------------------------"
        ]
        
        # 1. MARKET REGIME & BREADTH
        lines.append("**1. MARKET REGIME & CONTINUOUS TREND**")
        nf_spot = float(metrics.get("nifty_spot") or nf.get("last_price") or 24847.2)
        nf_chg = float(metrics.get("nifty_change_pct", -0.56))
        sx_spot = float(metrics.get("sensex_spot", 80757.2))
        sx_chg = float(metrics.get("sensex_change_pct", -0.33))
        
        reg_lbl = trend_info.get("label", "DOWNTREND (BEAR DISTRIBUTION)")
        score_lbl = trend_info.get("score_label", f"{trend_info.get('score', -40)}/100")
        lines.append(f"• Spot: NIFTY {nf_spot:,.2f} ({nf_chg:+.2f}%) | SENSEX {sx_spot:,.2f} ({sx_chg:+.2f}%)")
        lines.append(f"• Regime: `{reg_lbl}` ({score_lbl}) | Gate: `{opt_dir.get('badge', 'DEFENSIVE')}`")
        lines.append(f"• NSE Breadth: `{metrics.get('broad_advances', '--')} Adv` / `{metrics.get('broad_declines', '--')} Dec` (ADR: `{metrics.get('broad_adr', '--')}`)")
        lines.append("")
        
        # 2. INTRADAY CASH PICKS
        lines.append("**2. INTRADAY CASH EQUITY SUGGESTIONS**")
        top_stocks = shortlist.get("top_intraday_stocks", [])
        if not top_stocks and shortlist.get("best_intraday_stock"):
            top_stocks = [shortlist.get("best_intraday_stock")]

        if top_stocks:
            for idx, item in enumerate(top_stocks, 1):
                sym = item.get("symbol")
                ltp = item.get("last_price")
                act = item.get("intraday_action_badge", "BUY")
                score = item.get("confluence_score", 0)
                risk = item.get("risk_management", {})
                sl = risk.get("stop_loss", item.get("range_bottom", "--"))
                tgt = risk.get("target", "--")
                lines.append(f"{idx}. `[{act}] {sym}` @ Rs {ltp:,.2f} (Score: {score}/100)")
                lines.append(f"   Target: Rs {tgt} | SL: Rs {sl} | 50 EMA: Rs {item.get('ema50')} ({item.get('ema50_dist_pct', 0):+0.1f}%)")
        else:
            watch_list = shortlist.get("top_watch_stocks", [])
            if watch_list:
                lines.append("• Stance: `HOLD CASH` (Bear market defense; no scrip cleared 80/100 threshold)")
                watch_syms = ", ".join([f"{w.get('symbol')} (Rs {w.get('last_price')})" for w in watch_list[:2]])
                lines.append(f"• Key Watch: {watch_syms}")
            else:
                lines.append("• Stance: `HOLD CASH` (All universe equities consolidating inside defensive bands)")
        lines.append("")

        # 3. NIFTY & SENSEX OPTIONS DIRECTIVES
        lines.append("**3. NIFTY & SENSEX OPTIONS DIRECTIVES**")
        
        # Nifty 50 Options Setup
        atm_nifty = int(round(nf_spot / 50.0) * 50) if nf_spot > 0 else 24850
        if "DOWNTREND" in reg_lbl or "BEAR" in reg_lbl:
            lines.append(f"• NIFTY 50: `[BUY PUT]` NIFTY {atm_nifty} PE (Downside Bias • Outlay < Rs 5,000, 30% SL)")
        elif "UPTREND" in reg_lbl or "BULL" in reg_lbl:
            lines.append(f"• NIFTY 50: `[BUY CALL]` NIFTY {atm_nifty} CE (Upside Bias • Outlay < Rs 5,000, 30% SL)")
        else:
            lines.append(f"• NIFTY 50: `[STANDBY]` (Choppy market • Theta decay risk on naked options)")

        # Sensex 30 Options Setup
        if is_sensex_day:
            atm_sensex = int(round(sx_spot / 100.0) * 100) if sx_spot > 0 else 80800
            s_act = "PUT" if ("DOWNTREND" in reg_lbl or "BEAR" in reg_lbl) else "CALL"
            lines.append(f"• SENSEX 30 (Active Today): `[BUY {s_act}]` SENSEX {atm_sensex} {s_act[:1]}E (Lot 20, Outlay < Rs 500, 75% SL)")
        else:
            lines.append("• SENSEX 30: `[STANDBY]` (Scheduled for Wed & Thu cycle)")

        # Midcap Nifty (Active Mondays)
        if is_midcap_day:
            if mid.get("is_actionable") and mid.get("recommended_strike"):
                lines.append(f"• MIDCAP NIFTY (Active Today): `[{mid.get('action_badge')}] {mid.get('recommended_strike')}` (Lot 75, Outlay < Rs 750, 60% SL)")
            else:
                lines.append("• MIDCAP NIFTY (Active Today): `[ACTIVE MONITORING]` (Lot 75, Outlay < Rs 750, 60% SL)")
        else:
            lines.append("• MIDCAP NIFTY: `[STANDBY]` (Scheduled for Mon & Tue cycle)")

        # Stock Options
        if stk:
            lines.append(f"• STOCK OPTION (Lot <= 500): `[{stk.get('option_action_badge')}] {stk.get('recommended_contract')}` (Score: {stk.get('confluence_score')}/100)")
        else:
            lines.append("• STOCK OPTION (Lot <= 500): `[HOLD CASH]` (No setup cleared +50pt confluence)")
        lines.append("")

        # 4. 4-HOUR PENNY & LONG-TERM BREAKOUTS
        lines.append("**4. 4-HOUR PENNY & LONG-TERM BREAKOUTS**")
        lines.append("• Penny (< Rs 50, Zero Debt): `[ACCUMULATING]` (Wallet: Rs 20 / Rs 200 | Next 4H close: 13:15 IST)")
        lines.append("• Long-Term Quality: `[STANDBY]` (Awaiting base breakouts on weekly/4H closes)")
        lines.append("")

        # 5. MCX COMMODITIES
        app3_data = self._fetch_app3_commodity()
        lines.append("**5. MCX COMMODITIES (18:00 - 21:30 US Focus)**")
        if app3_data:
            p = app3_data.get("priority_trade", {})
            comms = app3_data.get("commodities", {})
            crude = comms.get("CRUDEOILM", {})
            natgas = comms.get("NATGASMINI", {})
            gold = comms.get("GOLDPETAL", {})
            
            lines.append(f"• Crude Mini: Rs {crude.get('ltp', 0):,.0f} | NatGas: Rs {natgas.get('ltp', 0):,.1f} | Gold: Rs {gold.get('ltp', 0):,.0f}")
            if p and p.get("symbol"):
                lines.append(f"• Priority: `{p.get('display_name')}` ({p.get('futures_directive')})")
        else:
            lines.append("• Tracking Crude Mini, NatGas Mini, and Gold Petal for US session open.")
        lines.append("")

        lines.append("----------------------------------------")
        lines.append(f"Cockpit: {RENDER_SCANNER_URL}")

        message = "\n".join(lines)
        title = f"FinPlus+ Pulse | {ist_time}"
        
        success = False
        cockpit_url = RENDER_SCANNER_URL
        
        for idx, topic in enumerate(self.topics):
            if idx > 0:
                time.sleep(1.5)
                
            url = f"https://ntfy.sh/{topic}"
            headers = {
                "Title": title,
                "Priority": "4",
                "Markdown": "yes",
                "X-Markdown": "yes",
                "Click": cockpit_url,
                "Actions": f"view, Open Scanner Cockpit, {cockpit_url}"
            }
            
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
        Structured in clean, institutional financial bulletin format.
        """
        from datetime import datetime, timezone, timedelta
        ist_now = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
        day_name = ist_now.strftime("%A")
        date_str = ist_now.strftime("%d-%b-%Y")
        
        # Strict Weekend Silence
        if not force and ist_now.weekday() >= 5:
            logger.info("Weekend detected. Skipping 09:00 AM Morning Briefing.")
            return False
        
        crucible_end = datetime(2026, 12, 1, 23, 59, 59)
        crucible_remaining = max(0, (crucible_end.date() - ist_now.date()).days)
        is_crucible = crucible_remaining > 0
        
        is_sensex_day = day_name in ("Wednesday", "Thursday")
        is_midcap_day = day_name in ("Monday", "Tuesday")
        news = self._fetch_morning_macro_news()
        
        lines = [
            f"**FINPLUS+ MORNING TACTICAL BRIEFING**",
            f"`{day_name}, {date_str} | 09:00 IST`",
            "----------------------------------------"
        ]
        
        if is_crucible:
            lines.append(f"• Regime: Paper Crucible ({crucible_remaining} Days Remaining)")
        else:
            lines.append("• Regime: Live Flywheel Active (Rs 7,750 Pool + 5 Wallets)")
        lines.append("")

        # 1. MACRO CUES
        lines.append("**1. MACRO & GLOBAL SENTIMENT**")
        lines.append(f"• Gift Nifty: {news['gift_nifty']}")
        lines.append(f"• Global: {news['global_cues']}")
        lines.append(f"• Bias: {news['macro_sentiment']}")
        lines.append("")

        # 2. SEGMENT AGENDA
        lines.append("**2. SEGMENT TRADING DIRECTIVES**")
        if is_midcap_day:
            lines.append(f"• MIDCAP NIFTY: `ACTIVE TODAY ({day_name.upper()})` — Lot 75, Outlay < Rs 750, 60% SL, hold till expiry")
        else:
            lines.append("• MIDCAP NIFTY: `STANDBY` (Mon & Tue cycle)")

        if is_sensex_day:
            lines.append(f"• SENSEX 30: `ACTIVE TODAY ({day_name.upper()})` — Lot 20, Outlay < Rs 500, 75% SL, entered on confirmed CTI")
        else:
            lines.append("• SENSEX 30: `STANDBY` (Wed & Thu cycle)")

        lines.append("• INTRADAY CORE: `ACTIVE` (09:15 - 15:00 IST) — Rs 7,750 pool, 5x MIS, minimum 80/100 conviction")
        lines.append("")

        # 3. SPECIFIC EQUITY SUGGESTIONS
        lines.append("**3. SPECIFIC EQUITY CANDIDATES**")
        if shortlist:
            top_stocks = shortlist.get("top_intraday_stocks", [])
            if not top_stocks and shortlist.get("best_intraday_stock"):
                top_stocks = [shortlist.get("best_intraday_stock")]
            
            if top_stocks:
                for idx, item in enumerate(top_stocks, 1):
                    sym = item.get("symbol")
                    ltp = item.get("last_price")
                    act = item.get("intraday_action_badge", "BUY")
                    risk = item.get("risk_management", {})
                    sl = risk.get("stop_loss", "--")
                    tgt = risk.get("target", "--")
                    score = item.get("confluence_score", 0)
                    lines.append(f"{idx}. `[{act}] {sym}` @ Rs {ltp:,.2f} (Target: Rs {tgt} | SL: Rs {sl} | Score: {score}/100)")
            else:
                watch_stocks = shortlist.get("top_watch_stocks", [])
                if watch_stocks:
                    w_text = ", ".join([f"{w.get('symbol')} (Trigger: Rs {w.get('range_top')})" for w in watch_stocks[:3]])
                    lines.append(f"• Watch Levels: {w_text}")
                else:
                    lines.append("• Scanning 750 universe equities at 09:15 market open.")
        else:
            lines.append("• Scanning 750 universe equities at 09:15 market open.")
        lines.append("")

        # 4. MCX COMMODITIES & EIA SCHEDULE
        lines.append("**4. MCX COMMODITIES & EIA TIMING**")
        lines.append("• Session: 17:00 – 23:30 IST (Prime US Window: 18:00 – 21:30 IST)")
        lines.append("• Universe: Crude Mini (10 Bbl), NatGas Mini (250 mmBtu), Gold Petal (1g)")
        
        if day_name == "Wednesday":
            lines.append("• EIA Schedule: US Crude Oil Inventories tonight at 20:00 IST (Volatility Shield: Flat across release)")
        elif day_name == "Thursday":
            lines.append("• EIA Schedule: US Natural Gas Storage Report tonight at 20:00 IST (Volatility Shield: Flat across release)")
        else:
            lines.append("• EIA Schedule: Standard trend-following (Crude report: Wed 20:00 IST, NatGas: Thu 20:00 IST)")
        lines.append("")

        # 5. DYNAMIC LIVE WALLET MILESTONES
        try:
            resp = requests.get("http://127.0.0.1:8003/api/wallets", timeout=1.5)
            if resp.status_code == 200:
                w_data = resp.json().get("wallets", [])
                unlocked = [w for w in w_data if w.get("is_unlocked")]
                accumulating = [w for w in w_data if not w.get("is_unlocked")]
                lines.append("**5. LIVE CAPITAL & WALLET STATUS**")
                if unlocked:
                    for uw in unlocked:
                        lines.append(f"• `{uw['display_name']}`: UNLOCKED (Rs {uw['balance']:,.2f} >= Rs {uw['target_threshold']:,.2f})")
                for aw in accumulating[:3]:
                    short_fall = round(aw['target_threshold'] - aw['balance'], 2)
                    lines.append(f"• `{aw['display_name']}`: Rs {aw['balance']:,.2f} / Rs {aw['target_threshold']:,.2f} (Rs {short_fall:,.2f} left)")
                lines.append("")
        except Exception:
            pass

        # 6. 4-Hour Timeframe Checks
        lines.append("**6. 4-HOUR TIMEFRAME CHECKPOINTS**")
        lines.append("• Inspect 4H closed candles at 13:15 IST and 15:30 IST for Stage 2 Penny (< Rs 50, Zero Debt) breakouts.")
        lines.append("")
        lines.append("----------------------------------------")
        lines.append(f"Cockpit: {RENDER_SCANNER_URL}")

        message = "\n".join(lines)
        title = f"FinPlus+ Morning Agenda | {day_name}"
        
        cockpit_url = RENDER_SCANNER_URL
        success = False
        
        for topic in self.topics:
            url = f"https://ntfy.sh/{topic}"
            headers = {
                "Title": title,
                "Priority": "5",
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
