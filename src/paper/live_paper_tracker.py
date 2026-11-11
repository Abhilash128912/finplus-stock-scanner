"""
FinPlus+ 24/7 Cloud Live Paper Trading Tracker
Embedded in App #2 (Stock Scanner & Options Ignition Engine)
Runs 24/7 on Render Cloud ($0 Free Tier, 0 extra instances)
Supports mobile and desktop FrontPage-style live paper cockpit.
Strict ATM Options Modeling, Dynamic Delta, Theta Decay & Dynamic Trailing SL.
"""

from __future__ import annotations

import asyncio
import datetime
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
import yfinance as yf

from src.utils.time_and_logger import setup_logger, get_utc_iso, get_ist_time_str, get_ist_now
from src.utils.indian_market_calendar import is_equity_market_open_ist, is_mcx_market_open_ist

logger = setup_logger("LivePaperTracker")

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
VIRTUAL_STARTING_CAPITAL = 100_000.0  # ₹1 Lakh Virtual Sandbox Capital
MAX_CONCURRENT_PER_SEGMENT = 6        # Ingest all distinct signals simultaneously


class LivePaperTracker:
    def __init__(self, db_path: Optional[str] = None):
        if not db_path:
            base_dir = Path(__file__).resolve().parent.parent.parent
            data_dir = base_dir / "data"
            data_dir.mkdir(parents=True, exist_ok=True)
            self.db_path = str(data_dir / "paper_trading.db")
        else:
            self.db_path = db_path

        self.virtual_capital = VIRTUAL_STARTING_CAPITAL
        self.active_positions: Dict[str, Dict[str, Any]] = {}
        self.is_running = False
        self._init_db()
        self._load_open_positions()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        """Initializes tables for active paper positions and closed trade ledger."""
        with self._get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS active_paper_positions (
                    position_id TEXT PRIMARY KEY,
                    source_app TEXT NOT NULL,
                    sector TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    action TEXT NOT NULL,
                    is_option INTEGER NOT NULL DEFAULT 0,
                    is_put INTEGER NOT NULL DEFAULT 0,
                    entry_price REAL NOT NULL,
                    current_price REAL NOT NULL,
                    initial_sl REAL NOT NULL,
                    trailing_sl REAL NOT NULL,
                    target REAL NOT NULL,
                    quantity INTEGER NOT NULL,
                    risk_r REAL NOT NULL,
                    opt_entry_premium REAL NOT NULL DEFAULT 0.0,
                    opt_current_premium REAL NOT NULL DEFAULT 0.0,
                    opt_initial_sl REAL NOT NULL DEFAULT 0.0,
                    opt_trailing_sl REAL NOT NULL DEFAULT 0.0,
                    opt_target REAL NOT NULL DEFAULT 0.0,
                    stage TEXT NOT NULL,
                    status TEXT NOT NULL,
                    entry_time_ist TEXT NOT NULL,
                    opened_at_utc TEXT NOT NULL,
                    notes TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS closed_paper_trades (
                    trade_id TEXT PRIMARY KEY,
                    position_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    sector TEXT NOT NULL,
                    action TEXT NOT NULL,
                    is_option INTEGER NOT NULL,
                    entry_price REAL NOT NULL,
                    exit_price REAL NOT NULL,
                    quantity INTEGER NOT NULL,
                    gross_pnl REAL NOT NULL,
                    total_charges REAL NOT NULL,
                    net_pnl REAL NOT NULL,
                    exit_reason TEXT NOT NULL,
                    entry_time_ist TEXT NOT NULL,
                    exit_time_ist TEXT NOT NULL,
                    opened_at_utc TEXT NOT NULL,
                    closed_at_utc TEXT NOT NULL
                )
            """)
            conn.commit()

    def _load_open_positions(self):
        """Loads open positions from database on server startup."""
        try:
            with self._get_connection() as conn:
                rows = conn.execute("SELECT * FROM active_paper_positions WHERE status = 'OPEN'").fetchall()
                for r in rows:
                    pos = dict(r)
                    self.active_positions[pos["position_id"]] = pos
                logger.info(f"Loaded {len(self.active_positions)} active open paper positions from DB.")
        except Exception as e:
            logger.error(f"Error loading open positions: {e}")

    def open_position(
        self,
        source_app: str,
        sector: str,
        symbol: str,
        action: str,
        entry_price: float,
        stop_loss: float,
        target: float,
        quantity: int = 1,
        notes: str = ""
    ) -> Optional[Dict[str, Any]]:
        """Opens a new paper trade with realistic ATM option premium and dynamic trailing."""
        now_ist = datetime.datetime.now(tz=IST)
        curr_time = now_ist.time()
        is_weekday = now_ist.weekday() < 5

        # Check market hours
        if sector in ("INTRADAY_CASH", "STOCK_OPTIONS", "INDEX_OPTIONS", "SENSEX_ASYMMETRIC", "NIFTY_ASYMMETRIC", "MIDCAP_ASYMMETRIC"):
            if not is_weekday or not (datetime.time(9, 15) <= curr_time < datetime.time(15, 15)):
                logger.info(f"Market Closed: {symbol} ({sector}) rejected outside NSE hours.")
                return None
        elif sector == "COMMODITIES":
            if not is_weekday or not (datetime.time(9, 0) <= curr_time < datetime.time(23, 30)):
                logger.info(f"Market Closed: {symbol} ({sector}) rejected outside MCX hours.")
                return None

        # Max open positions per segment
        open_in_sector = sum(1 for p in self.active_positions.values() if p["sector"] == sector and p["status"] == "OPEN")
        if open_in_sector >= MAX_CONCURRENT_PER_SEGMENT:
            return None

        clean_base = symbol.split(" ")[0].upper()
        # Avoid duplicate open positions on same symbol
        for p in self.active_positions.values():
            if p["symbol"].split(" ")[0].upper() == clean_base and p["status"] == "OPEN":
                return None

        pos_id = f"{clean_base}_{action}_{sector}_{now_ist.strftime('%Y%m%d_%H%M%S')}"
        is_option = sector in ("STOCK_OPTIONS", "INDEX_OPTIONS", "SENSEX_ASYMMETRIC", "NIFTY_ASYMMETRIC", "MIDCAP_ASYMMETRIC")
        
        is_put = False
        if is_option:
            tokens = symbol.upper().split()
            is_put = ("PE" in tokens) or ("PUT" in tokens) or ("SELL" in action.upper())

        risk_r = abs(entry_price - stop_loss)
        if risk_r <= 0:
            risk_r = entry_price * 0.015

        # Realistic ATM Option Premium Model (Capital Outlay ~₹2,700 - ₹5,000)
        if is_option:
            if "SENSEX" in clean_base:
                opt_entry_prem = 180.0
            elif "NIFTY" in clean_base and "MIDCAP" not in clean_base and "MIDCPNIFTY" not in clean_base:
                opt_entry_prem = 110.0
            elif "MIDCPNIFTY" in clean_base or "MIDCAP" in clean_base:
                opt_entry_prem = 55.0
            else:
                opt_entry_prem = max(2.0, round(entry_price * 0.012, 2))
            opt_sl = round(opt_entry_prem * 0.70, 2)   # -30% SL
            opt_tgt = round(opt_entry_prem * 1.60, 2)  # +60% Target (1:2 R:R)
        else:
            opt_entry_prem = 0.0
            opt_sl = stop_loss
            opt_tgt = target

        now_ist_str = get_ist_now().strftime("%Y-%m-%d %I:%M:%S %p IST")
        opened_utc = get_utc_iso()

        position = {
            "position_id": pos_id,
            "source_app": source_app,
            "sector": sector,
            "symbol": symbol,
            "action": action.upper(),
            "is_option": 1 if is_option else 0,
            "is_put": 1 if is_put else 0,
            "entry_price": round(entry_price, 2),
            "current_price": round(entry_price, 2),
            "initial_sl": round(stop_loss, 2),
            "trailing_sl": round(stop_loss, 2),
            "target": round(target, 2),
            "quantity": quantity,
            "risk_r": round(risk_r, 2),
            "opt_entry_premium": opt_entry_prem,
            "opt_current_premium": opt_entry_prem,
            "opt_initial_sl": opt_sl,
            "opt_trailing_sl": opt_sl,
            "opt_target": opt_tgt,
            "stage": "INITIAL (FIXED SL)",
            "status": "OPEN",
            "entry_time_ist": now_ist_str,
            "opened_at_utc": opened_utc,
            "unrealized_pnl": 0.0,
            "pnl_pct": 0.0,
            "notes": notes
        }

        with self._get_connection() as conn:
            conn.execute("""
                INSERT INTO active_paper_positions (
                    position_id, source_app, sector, symbol, action,
                    is_option, is_put, entry_price, current_price,
                    initial_sl, trailing_sl, target, quantity, risk_r,
                    opt_entry_premium, opt_current_premium, opt_initial_sl,
                    opt_trailing_sl, opt_target, stage, status,
                    entry_time_ist, opened_at_utc, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                pos_id, source_app, sector, symbol, action.upper(),
                position["is_option"], position["is_put"],
                position["entry_price"], position["current_price"],
                position["initial_sl"], position["trailing_sl"],
                position["target"], quantity, position["risk_r"],
                opt_entry_prem, opt_entry_prem, opt_sl,
                opt_sl, opt_tgt, position["stage"], "OPEN",
                now_ist_str, opened_utc, notes
            ))
            conn.commit()

        self.active_positions[pos_id] = position
        logger.info(f"PAPER TRADE OPENED: [{action}] {symbol} ({sector}) @ ₹{entry_price:.2f} | Qty: {quantity}")
        return position

    def ingest_shortlist(self, shortlist: Dict[str, Any]):
        """Auto-ingests all qualified trades from the scanner's 30-min shortlist."""
        if not shortlist:
            return

        # 1. Intraday Cash
        cash = shortlist.get("best_intraday_stock")
        if cash and cash.get("confluence_score", 0) >= 80 and "APPROVED" in cash.get("regime_confluence", ""):
            sym = cash.get("symbol")
            pts = cash.get("points_breakdown", {})
            vol_pts = pts.get("volume_pts", 0)
            rvol = float(cash.get("relative_volume", 0.0) or 0.0)
            if not (vol_pts < 10 and (rvol > 0 and rvol < 1.0)):
                ltp = float(cash.get("last_price", 0.0))
                risk = cash.get("risk_management", {})
                sl = float(risk.get("stop_loss", ltp * 0.985))
                tgt = float(risk.get("target", ltp * 1.03))
                act = "SELL" if "SELL" in cash.get("intraday_action_badge", "") else "BUY"
                qty = max(1, int(40_000 / ltp)) if ltp > 0 else 25
                self.open_position(
                    source_app="APP_02_STOCK_SCANNER",
                    sector="INTRADAY_CASH",
                    symbol=sym,
                    action=act,
                    entry_price=ltp,
                    stop_loss=sl,
                    target=tgt,
                    quantity=qty,
                    notes=f"Cash Breakout: {cash.get('reason', '')}"
                )

        # 2. Stock Option
        stk_opt = shortlist.get("best_stock_option")
        if stk_opt and stk_opt.get("confluence_score", 0) >= 65 and "APPROVED" in stk_opt.get("regime_confluence", ""):
            sym = stk_opt.get("recommended_contract") or f"{stk_opt.get('symbol')} ATM"
            opt_pts = stk_opt.get("points_breakdown", {})
            opt_vol_pts = opt_pts.get("volume_pts", 0)
            opt_rvol = float(stk_opt.get("relative_volume", 0.0) or 0.0)
            if not (opt_vol_pts < 10 and (opt_rvol > 0 and opt_rvol < 1.0)):
                underlying_ltp = float(stk_opt.get("last_price", 0.0))
                risk = stk_opt.get("risk_management", {})
                u_sl = float(risk.get("underlying_sl", underlying_ltp * 1.015))
                u_tgt = float(risk.get("underlying_target", underlying_ltp * 0.97))
                lot_size = int(stk_opt.get("lot_size", 400))
                self.open_position(
                    source_app="APP_02_STOCK_SCANNER",
                    sector="STOCK_OPTIONS",
                    symbol=sym,
                    action="BUY",
                    entry_price=underlying_ltp,
                    stop_loss=u_sl,
                    target=u_tgt,
                    quantity=lot_size,
                    notes=f"Stock Option: {stk_opt.get('reason', '')}"
                )

        # 3. Sensex Radar
        sx = shortlist.get("sensex_radar", {})
        if sx.get("is_actionable") and sx.get("recommended_strike"):
            sx_strike = sx["recommended_strike"]
            sx_ltp = float(sx.get("last_price", 82300.0))
            lot = int(sx.get("lot_size", 20))
            self.open_position(
                source_app="APP_02_STOCK_SCANNER",
                sector="SENSEX_ASYMMETRIC",
                symbol=sx_strike,
                action="BUY",
                entry_price=sx_ltp,
                stop_loss=sx_ltp * 0.99,
                target=sx_ltp * 1.02,
                quantity=lot,
                notes="Sensex Radar Ticket"
            )

        # 4. Nifty Radar
        nf = shortlist.get("nifty_radar", {})
        if nf.get("is_actionable") and nf.get("recommended_strike"):
            nf_strike = nf["recommended_strike"]
            nf_ltp = float(nf.get("last_price", 25380.0))
            lot = int(nf.get("lot_size", 25))
            self.open_position(
                source_app="APP_02_STOCK_SCANNER",
                sector="NIFTY_ASYMMETRIC",
                symbol=nf_strike,
                action="BUY",
                entry_price=nf_ltp,
                stop_loss=nf_ltp * 0.99,
                target=nf_ltp * 1.02,
                quantity=lot,
                notes="Nifty Radar Ticket"
            )

        # 5. Midcap Nifty Radar
        mid = shortlist.get("midcap_radar", {})
        if mid.get("is_actionable") and mid.get("recommended_strike"):
            mid_strike = mid["recommended_strike"]
            mid_ltp = float(mid.get("last_price", 12500.0))
            lot = int(mid.get("lot_size", 50))
            self.open_position(
                source_app="APP_02_STOCK_SCANNER",
                sector="MIDCAP_ASYMMETRIC",
                symbol=mid_strike,
                action="BUY",
                entry_price=mid_ltp,
                stop_loss=mid_ltp * 0.99,
                target=mid_ltp * 1.02,
                quantity=lot,
                notes="Midcap Nifty Radar Ticket"
            )

    def _fetch_live_price(self, symbol: str, sector: str) -> Optional[float]:
        """Fetches live market price for trailing stop and P&L calculation."""
        clean_sym = symbol.split(" ")[0].upper()

        # Index Ticker resolution
        if "NIFTY" in clean_sym or "SENSEX" in clean_sym or "MIDCAP" in clean_sym or "MIDCPNIFTY" in clean_sym:
            try:
                # App 1 regime check
                rc = requests.get("http://127.0.0.1:8000/api/regime", timeout=1.0)
                if rc.status_code == 200:
                    metrics = rc.json().get("metrics", {})
                    if "SENSEX" in clean_sym:
                        val = metrics.get("sensex_spot")
                        if val and float(val) > 0:
                            return round(float(val), 2)
                    elif clean_sym == "NIFTY":
                        val = metrics.get("nifty_spot")
                        if val and float(val) > 0:
                            return round(float(val), 2)
            except Exception:
                pass

        index_map = {
            "NIFTY": "^NSEI",
            "SENSEX": "^BSESN",
            "BANKNIFTY": "^NSEBANK",
            "MIDCPNIFTY": "^NSEI",
        }

        try:
            yf_ticker = index_map.get(clean_sym, f"{clean_sym}.NS" if not clean_sym.startswith("^") else clean_sym)
            t = yf.Ticker(yf_ticker)
            fi = getattr(t, "fast_info", None)
            if fi:
                lp = fi.get("last_price")
                if lp and float(lp) > 0:
                    return round(float(lp), 2)
        except Exception:
            pass

        return None

    def update_and_trail_positions(self):
        """Updates live prices, P&L, applies trailing stop-loss, and checks exit triggers."""
        if not self.active_positions:
            return

        now_ist = datetime.datetime.now(tz=IST)
        hour = now_ist.hour
        minute = now_ist.minute
        is_equity_eod = (hour == 15 and minute >= 0) or (hour > 15)
        is_mcx_eod = (hour == 23 and minute >= 15) or (hour > 23)

        equity_sectors = (
            "INTRADAY_CASH", "STOCK_OPTIONS", "INDEX_OPTIONS",
            "SENSEX_ASYMMETRIC", "NIFTY_ASYMMETRIC", "MIDCAP_ASYMMETRIC"
        )

        for pos_id, pos in list(self.active_positions.items()):
            if pos["status"] != "OPEN":
                continue

            sym = pos["symbol"]
            act = pos["action"]
            entry = pos["entry_price"]
            r_val = pos["risk_r"]
            curr_sl = pos["trailing_sl"]
            tgt = pos["target"]
            sector = pos["sector"]
            is_opt = bool(pos.get("is_option"))
            is_put = bool(pos.get("is_put"))

            live_price = self._fetch_live_price(sym, sector)
            if not live_price or live_price <= 0:
                continue

            pos["current_price"] = live_price

            if is_opt:
                underlying_gain = (entry - live_price) if is_put else (live_price - entry)
                
                # Dynamic ATM Delta (0.50 -> 0.70 on acceleration, 0.20 on reversal)
                if underlying_gain > 0:
                    delta = min(0.70, 0.50 + (underlying_gain / entry) * 4)
                else:
                    delta = max(0.20, 0.50 + (underlying_gain / entry) * 4)

                # Intraday Theta Decay (~2.5% per hour)
                hours_held = 0.0
                try:
                    opened_dt = datetime.datetime.fromisoformat(pos.get("opened_at_utc", "").replace("Z", "+00:00"))
                    hours_held = max(0.0, (datetime.datetime.now(datetime.timezone.utc) - opened_dt).total_seconds() / 3600.0)
                except Exception:
                    pass
                theta_decay_pts = min(pos["opt_entry_premium"] * 0.40, hours_held * (pos["opt_entry_premium"] * 0.025))

                opt_gain_pts = (underlying_gain * delta) - theta_decay_pts
                opt_curr = max(0.05, round(pos["opt_entry_premium"] + opt_gain_pts, 2))
                pos["opt_current_premium"] = opt_curr

                # Unrealized P&L capped at 100% loss of premium paid
                unrealized = max(-pos["opt_entry_premium"] * pos["quantity"], round((opt_curr - pos["opt_entry_premium"]) * pos["quantity"], 2))
                opt_entry_val = pos.get("opt_entry_premium", 1.0)
                pos["pnl_pct"] = round(((pos["opt_current_premium"] - opt_entry_val) / opt_entry_val) * 100, 2) if opt_entry_val > 0 else 0.0
            else:
                if act == "BUY":
                    gain_pts = live_price - entry
                    unrealized = gain_pts * pos["quantity"]
                    pos["pnl_pct"] = round(((live_price - entry) / entry) * 100, 2) if entry > 0 else 0.0
                else:
                    gain_pts = entry - live_price
                    unrealized = gain_pts * pos["quantity"]
                    pos["pnl_pct"] = round(((entry - live_price) / entry) * 100, 2) if entry > 0 else 0.0

            pos["unrealized_pnl"] = round(unrealized, 2)

            # Trailing Stop-Loss & Exit Checks
            if is_opt:
                opt_curr = pos.get("opt_current_premium", 0.0)
                opt_entry = pos.get("opt_entry_premium", 1.0)
                opt_tgt = pos.get("opt_target", round(opt_entry * 1.60, 2))
                opt_sl = pos.get("opt_trailing_sl", round(opt_entry * 0.70, 2))

                opt_gain_pct = ((opt_curr - opt_entry) / opt_entry) if opt_entry > 0 else 0.0
                if opt_gain_pct >= 0.40:
                    new_sl = round(opt_entry * 1.25, 2)
                    if new_sl > opt_sl:
                        pos["opt_trailing_sl"] = new_sl
                        pos["stage"] = "TRAILED (LOCKING +25%)"
                elif opt_gain_pct >= 0.25:
                    new_sl = round(opt_entry, 2)
                    if new_sl > opt_sl:
                        pos["opt_trailing_sl"] = new_sl
                        pos["stage"] = "TRAILED (BREAKEVEN)"

                if opt_curr >= opt_tgt:
                    self.close_position(pos_id, exit_price=live_price, exit_reason="TARGET_HIT (+60% OPTION TARGET)")
                elif opt_curr <= pos.get("opt_trailing_sl", opt_sl):
                    reason = "TRAILING_SL_HIT" if pos["stage"] != "INITIAL (FIXED SL)" else "INITIAL_SL_HIT (-30% OPTION SL)"
                    self.close_position(pos_id, exit_price=live_price, exit_reason=reason)
                elif is_equity_eod and sector in equity_sectors:
                    self.close_position(pos_id, exit_price=live_price, exit_reason="3PM_MANDATORY_SQUAREOFF (15:00 IST)")

            else:
                r_multiple = gain_pts / r_val if r_val > 0 else 0.0
                is_down = is_put or act == "SELL"

                if is_down:
                    if r_multiple >= 1.5:
                        new_sl = round(entry - (0.75 * r_val), 2)
                        if new_sl < curr_sl:
                            pos["trailing_sl"] = new_sl
                            pos["stage"] = "TRAILED (LOCKING +0.75R)"
                    elif r_multiple >= 1.0:
                        new_sl = round(entry, 2)
                        if new_sl < curr_sl:
                            pos["trailing_sl"] = new_sl
                            pos["stage"] = "TRAILED (BREAKEVEN)"

                    if live_price <= tgt:
                        self.close_position(pos_id, exit_price=tgt, exit_reason="TARGET_HIT (1:2 R:R)")
                    elif live_price >= pos["trailing_sl"]:
                        reason = "TRAILING_SL_HIT" if pos["stage"] != "INITIAL (FIXED SL)" else "INITIAL_SL_HIT"
                        self.close_position(pos_id, exit_price=pos["trailing_sl"], exit_reason=reason)
                    elif is_equity_eod and sector in equity_sectors:
                        self.close_position(pos_id, exit_price=live_price, exit_reason="3PM_MANDATORY_SQUAREOFF (15:00 IST)")
                    elif is_mcx_eod and sector == "COMMODITIES":
                        self.close_position(pos_id, exit_price=live_price, exit_reason="EOD_MCX_CLOSE (23:15 IST)")
                else:
                    if r_multiple >= 1.5:
                        new_sl = round(entry + (0.75 * r_val), 2)
                        if new_sl > curr_sl:
                            pos["trailing_sl"] = new_sl
                            pos["stage"] = "TRAILED (LOCKING +0.75R)"
                    elif r_multiple >= 1.0:
                        new_sl = round(entry, 2)
                        if new_sl > curr_sl:
                            pos["trailing_sl"] = new_sl
                            pos["stage"] = "TRAILED (BREAKEVEN)"

                    if live_price >= tgt:
                        self.close_position(pos_id, exit_price=tgt, exit_reason="TARGET_HIT (1:2 R:R)")
                    elif live_price <= pos["trailing_sl"]:
                        reason = "TRAILING_SL_HIT" if pos["stage"] != "INITIAL (FIXED SL)" else "INITIAL_SL_HIT"
                        self.close_position(pos_id, exit_price=pos["trailing_sl"], exit_reason=reason)
                    elif is_equity_eod and sector in equity_sectors:
                        self.close_position(pos_id, exit_price=live_price, exit_reason="3PM_MANDATORY_SQUAREOFF (15:00 IST)")
                    elif is_mcx_eod and sector == "COMMODITIES":
                        self.close_position(pos_id, exit_price=live_price, exit_reason="EOD_MCX_CLOSE (23:15 IST)")

    def close_position(self, pos_id: str, exit_price: float, exit_reason: str):
        """Finalizes trade, calculates realistic broker taxes, and moves position to closed ledger."""
        if pos_id not in self.active_positions:
            return

        pos = self.active_positions[pos_id]
        pos["status"] = "CLOSED"
        pos["exit_price"] = round(exit_price, 2)
        pos["exit_reason"] = exit_reason

        is_opt = bool(pos.get("is_option"))
        qty = pos["quantity"]

        if is_opt:
            opt_entry = pos.get("opt_entry_premium", 1.0)
            opt_exit = pos.get("opt_current_premium", opt_entry)
            gross_pnl = round((opt_exit - opt_entry) * qty, 2)
            # Brokerage: ₹40 flat roundtrip + STT / turnover taxes ~₹15
            charges = 55.0
        else:
            entry = pos["entry_price"]
            if pos["action"] == "BUY":
                gross_pnl = round((exit_price - entry) * qty, 2)
            else:
                gross_pnl = round((entry - exit_price) * qty, 2)
            # Cash intraday charges ~₹45
            charges = 45.0

        net_pnl = round(gross_pnl - charges, 2)
        trade_id = f"TRD_{pos_id}"
        exit_time_ist = get_ist_now().strftime("%Y-%m-%d %I:%M:%S %p IST")
        closed_utc = get_utc_iso()

        with self._get_connection() as conn:
            conn.execute("""
                INSERT INTO closed_paper_trades (
                    trade_id, position_id, symbol, sector, action,
                    is_option, entry_price, exit_price, quantity,
                    gross_pnl, total_charges, net_pnl, exit_reason,
                    entry_time_ist, exit_time_ist, opened_at_utc, closed_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                trade_id, pos_id, pos["symbol"], pos["sector"], pos["action"],
                pos["is_option"], pos["entry_price"], pos["exit_price"], qty,
                gross_pnl, charges, net_pnl, exit_reason,
                pos["entry_time_ist"], exit_time_ist, pos["opened_at_utc"], closed_utc
            ))
            conn.execute("DELETE FROM active_paper_positions WHERE position_id = ?", (pos_id,))
            conn.commit()

        del self.active_positions[pos_id]
        logger.info(f"PAPER TRADE CLOSED: {pos['symbol']} -> Exit: ₹{exit_price:.2f} | {exit_reason} | Net P&L: ₹{net_pnl:,.2f}")

    def get_status_dict(self) -> Dict[str, Any]:
        """Returns the complete payload expected by the FrontPage live.html cockpit."""
        active_list = list(self.active_positions.values())
        tot_unrealized = round(sum(p.get("unrealized_pnl", 0.0) for p in active_list), 2)

        # Get closed trades from SQLite
        closed_trades = []
        tot_realized = 0.0
        try:
            with self._get_connection() as conn:
                rows = conn.execute("SELECT * FROM closed_paper_trades ORDER BY closed_at_utc DESC LIMIT 30").fetchall()
                for r in rows:
                    t = dict(r)
                    tot_realized += t.get("net_pnl", 0.0)
                    closed_trades.append({
                        "trade_id": t["trade_id"],
                        "symbol": t["symbol"],
                        "sector": t["sector"],
                        "action": t["action"],
                        "entry_price": t["entry_price"],
                        "exit_price": t["exit_price"],
                        "gross_pnl": t["gross_pnl"],
                        "total_charges": t["total_charges"],
                        "net_pnl": t["net_pnl"],
                        "exit_reason": t["exit_reason"],
                        "trade_time": t["exit_time_ist"]
                    })
        except Exception as e:
            logger.error(f"Error fetching closed trades: {e}")

        tot_realized = round(tot_realized, 2)
        current_equity = round(self.virtual_capital + tot_realized + tot_unrealized, 2)

        return {
            "status": "LIVE_ACTIVE",
            "virtual_starting_capital": self.virtual_capital,
            "current_equity": current_equity,
            "total_unrealized_pnl": tot_unrealized,
            "net_realized_pnl": tot_realized,
            "active_positions_count": len(active_list),
            "active_positions": active_list,
            "recent_closed_trades": closed_trades,
            "ist_time": get_ist_time_str(),
            "timestamp_utc": get_utc_iso()
        }

    async def start_polling_loop(self):
        """Asynchronous background loop for continuous live price trailing."""
        self.is_running = True
        logger.info("LivePaperTracker continuous polling loop started (3-second tick).")
        while self.is_running:
            try:
                await asyncio.to_thread(self.update_and_trail_positions)
                await asyncio.sleep(3)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in LivePaperTracker polling loop: {e}")
                await asyncio.sleep(5)

    def stop_polling_loop(self):
        self.is_running = False
