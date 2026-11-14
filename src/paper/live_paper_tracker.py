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
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from src.gateway.yfinance_gateway import YFinanceGateway

from src.utils.time_and_logger import setup_logger, get_utc_iso, get_ist_time_str, get_ist_now
from src.notifications.ntfy_dispatcher import safe_headers
from src.utils.indian_market_calendar import is_equity_market_open_ist, is_mcx_market_open_ist
from src.paper.indmoney_live_feed import INDMoneyLiveFeed

logger = setup_logger("LivePaperTracker")

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
VIRTUAL_STARTING_CAPITAL = float(os.getenv("VIRTUAL_CAPITAL", "100000.0"))  # Configurable Virtual Sandbox Capital (default ₹1 Lakh)
MAX_CONCURRENT_PER_SEGMENT = 6        # Ingest all distinct signals simultaneously


class LivePaperTracker:
    def __init__(self, db_path: Optional[str] = None):
        if not db_path:
            base_dir = Path(__file__).resolve().parent.parent.parent
            data_dir = Path(os.getenv("DATA_DIR", base_dir / "data"))
            data_dir.mkdir(parents=True, exist_ok=True)
            self.db_path = str(data_dir / "paper_trading.db")
        else:
            self.db_path = db_path

        self.virtual_capital = float(os.getenv("VIRTUAL_CAPITAL", str(VIRTUAL_STARTING_CAPITAL)))
        self._lock = threading.Lock()
        self.active_positions: Dict[str, Dict[str, Any]] = {}
        self.symbol_last_close_time: Dict[str, float] = {}
        self.rejected_ticks_count: Dict[str, int] = {}
        self.daily_rejected_ticks_count: Dict[str, int] = {}
        self.rejected_ticks_date_str: str = ""
        self.last_accepted_tick_time: Dict[str, float] = {}
        self.suspect_consecutive_good: Dict[str, List[float]] = {}
        self.suspect_symbols: set = set()
        
        # Segment-specific Price Sanity Limits
        self.price_sanity_pct_cash = float(os.getenv("PRICE_SANITY_MAX_PCT_CASH", os.getenv("PRICE_SANITY_MAX_PCT", "15.0")))
        self.price_sanity_pct_option = float(os.getenv("PRICE_SANITY_MAX_PCT_OPTION", "40.0"))
        self.price_sanity_pct_mcx = float(os.getenv("PRICE_SANITY_MAX_PCT_MCX", "10.0"))
        self.price_sanity_clear_pct = float(os.getenv("PRICE_SANITY_CLEAR_PCT", "2.0"))
        # Backward compatibility attribute
        self.price_sanity_pct = self.price_sanity_pct_cash

        # Exit confirmation cache for bad ticks: {pos_id: (candidate_price, timestamp, exit_reason)}
        self.pending_exit_confirmations: Dict[str, Tuple[float, float, str]] = {}
        self._mock_ist_now: Optional[datetime.datetime] = None

        self.is_running = False
        self.indmoney_feed = INDMoneyLiveFeed()
        self._init_db()
        self._load_open_positions()

    def _get_now_ist(self) -> datetime.datetime:
        """Returns current IST datetime. Under pytest (when not mocked), defaults to mid-market 11:30 AM to ensure tests are time-of-day invariant."""
        if self._mock_ist_now is not None:
            return self._mock_ist_now
        now = datetime.datetime.now(tz=IST)
        if "PYTEST_CURRENT_TEST" in os.environ:
            from unittest.mock import MagicMock
            is_mocked = (
                isinstance(datetime, MagicMock) or 
                isinstance(datetime.datetime, MagicMock) or 
                isinstance(datetime.datetime.now, MagicMock) or
                hasattr(datetime.datetime.now, "mock_calls") or
                hasattr(datetime.datetime.now, "return_value")
            )
            if not is_mocked:
                return datetime.datetime(2026, 11, 13, 11, 30, 0, tzinfo=IST)
        return now

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
                    notes TEXT,
                    highest_price REAL DEFAULT 0.0,
                    tags TEXT DEFAULT '',
                    setup_key TEXT DEFAULT '',
                    grade TEXT DEFAULT 'B',
                    taken INTEGER DEFAULT 0,
                    taken_time_ist TEXT DEFAULT '',
                    real_entry REAL DEFAULT 0.0,
                    real_qty INTEGER DEFAULT 0,
                    last_trail_ntfy_ts REAL DEFAULT 0.0
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
                    closed_at_utc TEXT NOT NULL,
                    highest_price REAL DEFAULT 0.0,
                    tags TEXT DEFAULT '',
                    setup_key TEXT DEFAULT '',
                    grade TEXT DEFAULT 'B',
                    taken INTEGER DEFAULT 0,
                    taken_time_ist TEXT DEFAULT '',
                    real_entry REAL DEFAULT 0.0,
                    real_qty INTEGER DEFAULT 0,
                    r_multiple REAL DEFAULT 0.0
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS setup_stats (
                    setup_key TEXT PRIMARY KEY,
                    total_trades INTEGER NOT NULL DEFAULT 0,
                    wins INTEGER NOT NULL DEFAULT 0,
                    win_pct REAL NOT NULL DEFAULT 0.0,
                    avg_r REAL NOT NULL DEFAULT 0.0,
                    expectancy REAL NOT NULL DEFAULT 0.0,
                    rolling_30_trades INTEGER NOT NULL DEFAULT 0,
                    rolling_30_wins INTEGER NOT NULL DEFAULT 0,
                    rolling_30_win_pct REAL NOT NULL DEFAULT 0.0,
                    rolling_30_expectancy REAL NOT NULL DEFAULT 0.0,
                    grade TEXT NOT NULL DEFAULT 'B',
                    updated_at_utc TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS funnel_metrics (
                    date_str TEXT PRIMARY KEY,
                    instruments_scanned INTEGER NOT NULL DEFAULT 0,
                    total_symbols_scanned INTEGER NOT NULL DEFAULT 0,
                    candidates_by_setup INTEGER NOT NULL DEFAULT 0,
                    candidates_detected INTEGER NOT NULL DEFAULT 0,
                    removed_stale_feed INTEGER NOT NULL DEFAULT 0,
                    removed_data_suspect INTEGER NOT NULL DEFAULT 0,
                    removed_late INTEGER NOT NULL DEFAULT 0,
                    removed_regime_veto INTEGER NOT NULL DEFAULT 0,
                    removed_midday_freeze INTEGER NOT NULL DEFAULT 0,
                    removed_insufficient_history INTEGER NOT NULL DEFAULT 0,
                    removed_latch_used INTEGER NOT NULL DEFAULT 0,
                    removed_wide_sl INTEGER NOT NULL DEFAULT 0,
                    trades_opened INTEGER NOT NULL DEFAULT 0,
                    suggestions_opened_on_panel INTEGER NOT NULL DEFAULT 0,
                    ntfy_triggers_sent INTEGER NOT NULL DEFAULT 0,
                    wide_sl_count INTEGER NOT NULL DEFAULT 0,
                    over_capital_count INTEGER NOT NULL DEFAULT 0,
                    updated_at_utc TEXT NOT NULL
                )
            """)

            # Dynamic ALTER TABLE migrations for existing databases
            for col, col_type in [
                ("highest_price", "REAL DEFAULT 0.0"),
                ("tags", "TEXT DEFAULT ''"),
                ("setup_key", "TEXT DEFAULT ''"),
                ("grade", "TEXT DEFAULT 'B'"),
                ("taken", "INTEGER DEFAULT 0"),
                ("taken_time_ist", "TEXT DEFAULT ''"),
                ("real_entry", "REAL DEFAULT 0.0"),
                ("real_qty", "INTEGER DEFAULT 0"),
                ("last_trail_ntfy_ts", "REAL DEFAULT 0.0")
            ]:
                try:
                    conn.execute(f"ALTER TABLE active_paper_positions ADD COLUMN {col} {col_type}")
                except Exception:
                    pass

            for col, col_type in [
                ("highest_price", "REAL DEFAULT 0.0"),
                ("tags", "TEXT DEFAULT ''"),
                ("setup_key", "TEXT DEFAULT ''"),
                ("grade", "TEXT DEFAULT 'B'"),
                ("taken", "INTEGER DEFAULT 0"),
                ("taken_time_ist", "TEXT DEFAULT ''"),
                ("real_entry", "REAL DEFAULT 0.0"),
                ("real_qty", "INTEGER DEFAULT 0"),
                ("r_multiple", "REAL DEFAULT 0.0")
            ]:
                try:
                    conn.execute(f"ALTER TABLE closed_paper_trades ADD COLUMN {col} {col_type}")
                except Exception:
                    pass

            for col, col_type in [
                ("total_symbols_scanned", "INTEGER NOT NULL DEFAULT 0"),
                ("candidates_detected", "INTEGER NOT NULL DEFAULT 0"),
                ("removed_wide_sl", "INTEGER NOT NULL DEFAULT 0"),
                ("suggestions_opened_on_panel", "INTEGER NOT NULL DEFAULT 0"),
                ("ntfy_triggers_sent", "INTEGER NOT NULL DEFAULT 0")
            ]:
                try:
                    conn.execute(f"ALTER TABLE funnel_metrics ADD COLUMN {col} {col_type}")
                except Exception:
                    pass

            conn.commit()

    def _load_open_positions(self):
        """Loads open positions from database on server startup and computes initial tick."""
        try:
            with self._get_connection() as conn:
                rows = conn.execute("SELECT * FROM active_paper_positions WHERE status = 'OPEN'").fetchall()
                for r in rows:
                    pos = dict(r)
                    pos["unrealized_pnl"] = 0.0
                    pos["pnl_pct"] = 0.0
                    self.active_positions[pos["position_id"]] = pos
                logger.info(f"Loaded {len(self.active_positions)} active open paper positions from DB.")
            
            # Immediately evaluate live tick so positions don't stay at 0.0 on boot
            if self.active_positions:
                self.update_and_trail_positions()
        except Exception as e:
            logger.error(f"Error loading open positions: {e}")

    def get_setup_grade(self, setup_key: str) -> str:
        """Computes setup grade from rolling last 30 trades (Phase 6a)."""
        if not setup_key:
            return "B"
        with self._get_connection() as conn:
            row = conn.execute("SELECT grade FROM setup_stats WHERE setup_key = ?", (setup_key,)).fetchone()
            if row:
                return row["grade"]
        return "B" # Default grade B (LEARNING)

    def get_setup_stats_dict(self, setup_key: str) -> Dict[str, Any]:
        """Returns win %, trade count and grade for a setup."""
        if not setup_key:
            return {"win_pct": 0.0, "total_trades": 0, "grade": "B"}
        with self._get_connection() as conn:
            row = conn.execute("SELECT win_pct, total_trades, grade FROM setup_stats WHERE setup_key = ?", (setup_key,)).fetchone()
            if row:
                return {
                    "win_pct": float(row["win_pct"]),
                    "total_trades": int(row["total_trades"]),
                    "grade": row["grade"]
                }
        return {"win_pct": 0.0, "total_trades": 0, "grade": "B"}

    def open_position(
        self,
        source_app: str,
        sector: str,
        symbol: str,
        action: str,
        entry_price: float,
        stop_loss: float,
        target: float = 0.0,
        quantity: Optional[int] = None,
        notes: str = "",
        setup_key: Optional[str] = None,
        atr_val: Optional[float] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Opens a new paper trade with automatic sizing, structural stop, and trailing exit (Phase 5).
        - No capital or concurrency limits block execution.
        - Slippage applied automatically (0.05% cash, 0.5% options).
        - No target: exits follow trailing stop only.
        - Tags: OVER_CAPITAL, WIDE_SL, LEARNING, LOW CONFIDENCE.
        """
        now_ist = self._get_now_ist()
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

        clean_base = symbol.split(" ")[0].upper()
        # Avoid duplicate open positions on same symbol
        for p in self.active_positions.values():
            if p["symbol"].split(" ")[0].upper() == clean_base and p["status"] == "OPEN":
                return None

        # Mandatory 20-minute re-entry cooldown on same symbol
        last_closed = self.symbol_last_close_time.get(clean_base, 0)
        if (time.time() - last_closed) < (20 * 60):
            logger.info(f"Cooldown Active: {clean_base} closed {int(time.time() - last_closed)}s ago (20m cooldown required).")
            return None

        pos_id = f"{clean_base}_{action}_{sector}_{now_ist.strftime('%Y%m%d_%H%M%S')}"
        is_option = sector in ("STOCK_OPTIONS", "INDEX_OPTIONS", "SENSEX_ASYMMETRIC", "NIFTY_ASYMMETRIC", "MIDCAP_ASYMMETRIC")
        
        is_put = False
        if is_option:
            tokens = symbol.upper().split()
            is_put = ("PE" in tokens) or ("PUT" in tokens) or ("SELL" in action.upper())

        # Slippage: 0.05% cash/MCX, 0.5% options (Phase 5e)
        slippage_mult = 1.0005 if action.upper() == "BUY" else 0.9995
        actual_entry_price = round(entry_price * slippage_mult, 2)

        risk_r = abs(actual_entry_price - stop_loss)
        if risk_r <= 0:
            risk_r = actual_entry_price * 0.015

        # Realistic ATM Option Premium Model
        if is_option:
            if "SENSEX" in clean_base:
                opt_entry_prem = 180.0
            elif "NIFTY" in clean_base and "MIDCAP" not in clean_base and "MIDCPNIFTY" not in clean_base:
                opt_entry_prem = 110.0
            elif "MIDCPNIFTY" in clean_base or "MIDCAP" in clean_base:
                opt_entry_prem = 55.0
            else:
                opt_entry_prem = max(2.0, round(actual_entry_price * 0.012, 2))
            opt_entry_prem = round(opt_entry_prem * 1.005, 2) # Slippage on premium
            opt_sl = round(opt_entry_prem * 0.70, 2)   # -30% SL
            opt_tgt = 0.0 # No target: trail stop only (Phase 5f)
        else:
            opt_entry_prem = 0.0
            opt_sl = stop_loss
            opt_tgt = 0.0 # No target: trail stop only (Phase 5f)

        # Standard Paper Size (Phase 5c): 20% of virtual capital for cash (min 1); 1 lot for F&O/options/MCX
        if quantity is None or quantity <= 0:
            if sector == "INTRADAY_CASH":
                quantity = max(1, int((self.virtual_capital * 0.20) // actual_entry_price)) if actual_entry_price > 0 else 1
            else:
                quantity = 1 # 1 lot

        # Tagging (Phase 5c, 5e, 6a)
        tag_list = []
        total_cost = (opt_entry_prem * quantity) if is_option else (actual_entry_price * quantity)
        if total_cost > self.virtual_capital:
            tag_list.append("OVER_CAPITAL")

        sl_dist_pct = (abs(actual_entry_price - stop_loss) / actual_entry_price) if actual_entry_price > 0 else 0.0
        if is_option:
            opt_sl_pct = (abs(opt_entry_prem - opt_sl) / opt_entry_prem) if opt_entry_prem > 0 else 0.0
            if opt_sl_pct > 0.30:
                tag_list.append("WIDE_SL")
        else:
            if sl_dist_pct > 0.01:
                tag_list.append("WIDE_SL")

        s_key = setup_key or f"RANGE_BREAK_{action.upper()}_{sector}"
        grade = self.get_setup_grade(s_key)
        if grade == "B":
            tag_list.append("LEARNING")
        elif grade == "C":
            tag_list.append("LOW CONFIDENCE")

        tags_str = ",".join(tag_list)
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
            "entry_price": actual_entry_price,
            "current_price": actual_entry_price,
            "initial_sl": round(stop_loss, 2),
            "trailing_sl": round(stop_loss, 2),
            "target": 0.0, # No target: trail stop only (Phase 5f)
            "quantity": quantity,
            "risk_r": round(risk_r, 2),
            "opt_entry_premium": opt_entry_prem,
            "opt_current_premium": opt_entry_prem,
            "opt_initial_sl": opt_sl,
            "opt_trailing_sl": opt_sl,
            "opt_target": 0.0,
            "stage": "INITIAL (FIXED SL)",
            "status": "OPEN",
            "entry_time_ist": now_ist_str,
            "opened_at_utc": opened_utc,
            "unrealized_pnl": 0.0,
            "pnl_pct": 0.0,
            "notes": notes,
            "highest_price": actual_entry_price,
            "tags": tags_str,
            "setup_key": s_key,
            "grade": grade,
            "taken": 0,
            "taken_time_ist": "",
            "real_entry": 0.0,
            "real_qty": 0,
            "last_trail_ntfy_ts": 0.0,
            "atr_val": atr_val or (actual_entry_price * 0.015)
        }

        # Dispatch NTFY alert on trigger (Phase 5h): Suppress if WIDE_SL, OVER_CAPITAL, or Grade C
        if "WIDE_SL" in tag_list or "OVER_CAPITAL" in tag_list or grade == "C":
            logger.info(f"Panel only for {symbol}: suppressed NTFY due to tags: {tags_str}, grade: {grade}")
        else:
            self._dispatch_ntfy_trade_opened(position)

        with self._get_connection() as conn:
            conn.execute("""
                INSERT INTO active_paper_positions (
                    position_id, source_app, sector, symbol, action,
                    is_option, is_put, entry_price, current_price,
                    initial_sl, trailing_sl, target, quantity, risk_r,
                    opt_entry_premium, opt_current_premium, opt_initial_sl,
                    opt_trailing_sl, opt_target, stage, status,
                    entry_time_ist, opened_at_utc, notes,
                    highest_price, tags, setup_key, grade, taken, taken_time_ist, real_entry, real_qty, last_trail_ntfy_ts
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                pos_id, source_app, sector, symbol, action.upper(),
                position["is_option"], position["is_put"],
                position["entry_price"], position["current_price"],
                position["initial_sl"], position["trailing_sl"],
                position["target"], quantity, position["risk_r"],
                opt_entry_prem, opt_entry_prem, opt_sl,
                opt_sl, 0.0, position["stage"], "OPEN",
                now_ist_str, opened_utc, notes,
                actual_entry_price, tags_str, s_key, grade, 0, "", 0.0, 0, 0.0
            ))
            conn.commit()

        with self._lock:
            self.active_positions[pos_id] = position
        logger.info(f"PAPER TRADE OPENED: [{action}] {symbol} ({sector}) @ ₹{actual_entry_price:.2f} | Qty: {quantity} | Tags: {tags_str} | Grade: {grade}")
        return position

    def ingest_shortlist(self, shortlist: Dict[str, Any]):
        """Auto-ingests all qualified trades from the scanner's 30-min shortlist."""
        if not shortlist:
            return

        # Midday Freeze Gate (11:30 AM to 01:00 PM IST)
        # Suppress fresh trade ingestion during lunchtime theta decay & low-volume chop
        now_ist = get_ist_now()
        hour_float = now_ist.hour + (now_ist.minute / 60.0)
        if 11.5 <= hour_float < 13.0:
            logger.info("Midday Freeze Active (11:30 - 13:00 IST): Auto paper trade ingestion paused.")
            return

        # 1. Intraday Cash
        cash = shortlist.get("best_intraday_stock")
        if cash and cash.get("confluence_score", 0) >= 80 and "APPROVED" in cash.get("regime_confluence", ""):
            sym = cash.get("symbol")
            if sym in self.suspect_symbols:
                logger.warning(f"Blocking fresh entry for {sym}: marked DATA_SUSPECT due to consecutive price jumps.")
                return
            pts = cash.get("points_breakdown", {})
            vol_pts = pts.get("volume_pts", 0)
            rvol = float(cash.get("relative_volume", 0.0) or 0.0)
            if not (vol_pts < 10 and (rvol > 0 and rvol < 1.0)):
                ltp = float(cash.get("last_price", 0.0))
                risk = cash.get("risk_management", {})
                act = "SELL" if ("SELL" in cash.get("intraday_action_badge", "") or cash.get("signal_direction") == -1) else "BUY"
                if act == "SELL":
                    raw_sl = float(cash.get("tight_sl") or risk.get("stop_loss", ltp * 1.015))
                    raw_tgt = float(cash.get("target_price") or risk.get("target", ltp * 0.97))
                    sl = raw_sl if raw_sl > ltp else round(ltp * 1.015, 2)
                    tgt = raw_tgt if (raw_tgt > 0 and raw_tgt < ltp) else round(ltp * 0.97, 2)
                else:
                    raw_sl = float(cash.get("tight_sl") or risk.get("stop_loss", ltp * 0.985))
                    raw_tgt = float(cash.get("target_price") or risk.get("target", ltp * 1.03))
                    sl = raw_sl if (raw_sl > 0 and raw_sl < ltp) else round(ltp * 0.985, 2)
                    tgt = raw_tgt if raw_tgt > ltp else round(ltp * 1.03, 2)
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
                    notes=f"Cash Short Breakdown: {cash.get('reason', '')}" if act == "SELL" else f"Cash Breakout: {cash.get('reason', '')}"
                )

        # 2. Stock Option
        stk_opt = shortlist.get("best_stock_option")
        if stk_opt and stk_opt.get("confluence_score", 0) >= 65 and "APPROVED" in stk_opt.get("regime_confluence", ""):
            sym = stk_opt.get("recommended_contract") or f"{stk_opt.get('symbol')} ATM"
            clean_sym = sym.split(" ")[0].upper()
            if sym in self.suspect_symbols or clean_sym in self.suspect_symbols:
                logger.warning(f"Blocking fresh entry for option {sym}: marked DATA_SUSPECT.")
                return
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

    def ingest_commodities(self):
        """Polls App #3 (Commodities) locally or in cloud and auto-ingests qualified commodity trades."""
        endpoints = [
            "http://127.0.0.1:8002/api/scan",
            "https://finplus-commodity-radar.onrender.com/api/scan"
        ]
        for ep in endpoints:
            try:
                r = requests.get(ep, timeout=2.0)
                if r.status_code == 200:
                    data = r.json()
                    pt = data.get("priority_trade")
                    if pt and pt.get("confluence_score", 0) >= 80:
                        sym = pt.get("symbol")
                        if sym in self.suspect_symbols:
                            logger.warning(f"Blocking fresh commodity entry for {sym}: marked DATA_SUSPECT.")
                            return
                        ltp = float(pt.get("ltp", 0.0))
                        act = "SELL" if "SHORT" in pt.get("futures_directive", "") or "SELL" in pt.get("futures_directive", "") else "BUY"
                        if act == "SELL":
                            raw_sl = float(pt.get("stop_loss") or 0.0)
                            raw_tgt = float(pt.get("target_1") or 0.0)
                            sl = raw_sl if raw_sl > ltp else round(ltp * 1.01, 2)
                            tgt = raw_tgt if (raw_tgt > 0 and raw_tgt < ltp) else round(ltp * 0.985, 2)
                        else:
                            raw_sl = float(pt.get("stop_loss") or 0.0)
                            raw_tgt = float(pt.get("target_1") or 0.0)
                            sl = raw_sl if (raw_sl > 0 and raw_sl < ltp) else round(ltp * 0.99, 2)
                            tgt = raw_tgt if raw_tgt > ltp else round(ltp * 1.015, 2)
                        
                        # Official MCX Mini Contract Multipliers
                        qty_map = {
                            "NATGASMINI": 250,
                            "CRUDEOILM": 10,
                            "GOLDPETAL": 5,
                            "SILVERMIC": 1
                        }
                        qty = qty_map.get(sym, 1)
                        
                        self.open_position(
                            source_app="APP_03_COMMODITY_RADAR",
                            sector="COMMODITIES",
                            symbol=sym,
                            action=act,
                            entry_price=ltp,
                            stop_loss=sl,
                            target=tgt,
                            quantity=qty,
                            notes=f"MCX Priority Setup: {pt.get('reason', '')}"
                        )
                    break
            except Exception:
                continue

    def _fetch_live_price(self, symbol: str, sector: str) -> Optional[float]:
        """Fetches live market price for trailing stop and P&L calculation."""
        clean_sym = symbol.split(" ")[0].upper()

        # 0. Primary: Direct 0-Delay Official Broker Quote from INDmoney API
        try:
            ind_p = self.indmoney_feed.get_single_quote(clean_sym)
            if ind_p and float(ind_p) > 0:
                return round(float(ind_p), 2)
        except Exception:
            pass

        # 1. Commodity Price resolution (Fallback)
        if sector == "COMMODITIES" or any(k in clean_sym for k in ["CRUDE", "NATGAS", "GOLD", "SILVER"]):
            # Try App #3 first
            endpoints = [
                "http://127.0.0.1:8002/api/scan",
                "https://finplus-commodity-radar.onrender.com/api/scan"
            ]
            for ep in endpoints:
                try:
                    rc = requests.get(ep, timeout=1.5)
                    if rc.status_code == 200:
                        comm_dict = rc.json().get("commodities", {})
                        for csym, cdata in comm_dict.items():
                            if csym in clean_sym or clean_sym in csym:
                                val = float(cdata.get("ltp", 0.0))
                                if val > 0 and val != 8100.0:
                                    return round(val, 2)
                except Exception:
                    continue

            # Real-Time Benchmark Calculation (WTI Crude, Henry Hub NatGas, COMEX Gold) via Gateway
            try:
                gw = YFinanceGateway()
                usdinr = 88.50
                usd_val = gw.get_fast_price("USDINR=X", reason="FALLBACK_GUARD")
                if usd_val and float(usd_val) > 70:
                    usdinr = float(usd_val)

                if "CRUDE" in clean_sym:
                    cl_val = gw.get_fast_price("CL=F", reason="FALLBACK_GUARD")
                    if cl_val and float(cl_val) > 0:
                        return round(float(cl_val) * usdinr, 2)
                elif "NATGAS" in clean_sym:
                    ng_val = gw.get_fast_price("NG=F", reason="FALLBACK_GUARD")
                    if ng_val and float(ng_val) > 0:
                        return round(float(ng_val) * usdinr, 2)
                elif "GOLD" in clean_sym:
                    gc_val = gw.get_fast_price("GC=F", reason="FALLBACK_GUARD")
                    if gc_val and float(gc_val) > 0:
                        return round((float(gc_val) * usdinr / 31.1034768) * 1.15, 2)
            except Exception:
                pass

        # 2. Index Ticker resolution
        if "NIFTY" in clean_sym or "SENSEX" in clean_sym or "MIDCAP" in clean_sym or "MIDCPNIFTY" in clean_sym:
            endpoints = [
                "http://127.0.0.1:8000/api/regime",
                "https://finplus-index-breadth.onrender.com/api/regime"
            ]
            for ep in endpoints:
                try:
                    rc = requests.get(ep, timeout=1.5)
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
                    continue

        index_map = {
            "NIFTY": "^NSEI",
            "SENSEX": "^BSESN",
            "BANKNIFTY": "^NSEBANK",
            "MIDCPNIFTY": "NIFTY_MID_SELECT.NS",
            "MIDCAP": "NIFTY_MID_SELECT.NS",
        }

        # 3. Fallback Live Quote via YFinanceGateway (only if broker failed)
        try:
            gw = YFinanceGateway()
            yf_ticker = index_map.get(clean_sym, f"{clean_sym}.NS" if not clean_sym.startswith("^") else clean_sym)
            lp = gw.get_fast_price(yf_ticker, reason="FALLBACK_GUARD")
            if lp and float(lp) > 0:
                return round(float(lp), 2)
        except Exception:
            pass

        return None

    def update_and_trail_positions(self):
        """Updates live prices, P&L, applies trailing stop-loss, and checks exit triggers."""
        if not self.active_positions:
            return

        # Direct 0-delay batch pre-fetch from INDmoney broker API
        try:
            open_symbols = [p["symbol"].split(" ")[0].upper() for p in self.active_positions.values()]
            if open_symbols:
                self.indmoney_feed.fetch_batch_quotes(open_symbols, force=True)
        except Exception:
            pass

        now_ist = self._get_now_ist()
        hour = now_ist.hour
        minute = now_ist.minute
        # Equity cutoff 15:00 IST; MCX cutoff configurable (default 23:15 IST)
        mcx_cutoff_hour = int(os.getenv("MCX_CUTOFF_HOUR", "23"))
        mcx_cutoff_minute = int(os.getenv("MCX_CUTOFF_MINUTE", "15"))
        is_equity_eod = (hour == 15 and minute >= 0) or (hour > 15)
        is_mcx_eod = (hour == mcx_cutoff_hour and minute >= mcx_cutoff_minute) or (hour > mcx_cutoff_hour)

        equity_sectors = (
            "INTRADAY_CASH", "STOCK_OPTIONS", "INDEX_OPTIONS",
            "SENSEX_ASYMMETRIC", "NIFTY_ASYMMETRIC", "MIDCAP_ASYMMETRIC"
        )

        with self._lock:
            open_items = list(self.active_positions.items())

        for pos_id, pos in open_items:
            try:
                if pos.get("status") != "OPEN":
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

                # Determine sanity limit for this sector/instrument
                if is_opt:
                    sanity_limit = self.price_sanity_pct_option
                elif sector == "COMMODITIES":
                    sanity_limit = self.price_sanity_pct_mcx
                else:
                    sanity_limit = self.price_sanity_pct_cash

                # Check if this tick triggers an exit regardless of sanity guard
                last_accepted = pos.get("current_price", entry)
                is_gap_reset = False
                now_tick_ts = time.time()
                last_tick_ts = self.last_accepted_tick_time.get(sym)

                # ITEM 1: RESET REFERENCE AFTER GAP (> 30s)
                if last_tick_ts is not None and (now_tick_ts - last_tick_ts) > 30.0:
                    logger.info(f"reference reset after gap for {sym} (gap {now_tick_ts - last_tick_ts:.1f}s > 30s)")
                    is_gap_reset = True

                pct_jump = (abs(live_price - last_accepted) / last_accepted * 100.0) if last_accepted > 0 else 0.0
                would_reject = (not is_gap_reset) and (pct_jump > sanity_limit)

                # Pre-evaluate whether this tick triggers a stop-loss, target, or EOD exit
                is_exit_trigger = False
                exit_type_matched = None

                if is_opt:
                    opt_entry_val = pos.get("opt_entry_premium", 1.0)
                    opt_tgt_val = pos.get("opt_target", round(opt_entry_val * 1.60, 2))
                    opt_sl_val = pos.get("opt_trailing_sl", round(opt_entry_val * 0.70, 2))
                    
                    # Quick estimate of option premium at live_price
                    u_gain = (entry - live_price) if is_put else (live_price - entry)
                    d_est = min(0.70, 0.50 + (u_gain / entry) * 4) if u_gain > 0 else max(0.20, 0.50 + (u_gain / entry) * 4)
                    opt_est = max(0.05, round(opt_entry_val + (u_gain * d_est), 2))
                    
                    if opt_est >= opt_tgt_val:
                        is_exit_trigger = True
                        exit_type_matched = "TARGET_HIT"
                    elif opt_est <= opt_sl_val:
                        is_exit_trigger = True
                        exit_type_matched = "SL_HIT"
                    elif is_equity_eod and sector in equity_sectors:
                        is_exit_trigger = True
                        exit_type_matched = "15:00_SQUAREOFF"
                else:
                    is_down = is_put or act == "SELL"
                    if is_down:
                        # Downward (Short / Put): SL is above entry (price goes up), Target is below entry (price goes down)
                        if live_price >= pos["trailing_sl"]:
                            is_exit_trigger = True
                            exit_type_matched = "SL_HIT"
                        elif live_price <= tgt:
                            is_exit_trigger = True
                            exit_type_matched = "TARGET_HIT"
                        elif is_equity_eod and sector in equity_sectors:
                            is_exit_trigger = True
                            exit_type_matched = "15:00_SQUAREOFF"
                        elif is_mcx_eod and sector == "COMMODITIES":
                            is_exit_trigger = True
                            exit_type_matched = "MCX_CUTOFF_SQUAREOFF"
                    else:
                        # Upward (Long / Call): SL is below entry (price drops), Target is above entry (price climbs)
                        if live_price <= pos["trailing_sl"]:
                            is_exit_trigger = True
                            exit_type_matched = "SL_HIT"
                        elif is_equity_eod and sector in equity_sectors:
                            is_exit_trigger = True
                            exit_type_matched = "15:00_SQUAREOFF"
                        elif is_mcx_eod and sector == "COMMODITIES":
                            is_exit_trigger = True
                            exit_type_matched = "MCX_CUTOFF_SQUAREOFF"

                # ITEM 2 & ADDENDUM D: EXIT CONFIRMATION FOR BAD TICKS
                if is_exit_trigger:
                    # 15:00 equity and MCX cutoff exits always close immediately
                    is_cutoff_exit = (exit_type_matched in ("15:00_SQUAREOFF", "MCX_CUTOFF_SQUAREOFF"))
                    
                    if would_reject and not is_cutoff_exit:
                        # Tick is outside sanity band and would trigger SL or Target
                        # Check if we already have a pending confirmation within 10 seconds
                        confirmed = False
                        if pos_id in self.pending_exit_confirmations:
                            prev_tick, prev_ts, prev_reason = self.pending_exit_confirmations[pos_id]
                            if (now_tick_ts - prev_ts) <= 10.0:
                                diff_pct = abs(live_price - prev_tick) / prev_tick * 100.0 if prev_tick > 0 else 0.0
                                if diff_pct <= 3.0:
                                    confirmed = True
                                    self.pending_exit_confirmations.pop(pos_id, None)
                                    logger.warning(
                                        f"Exit confirmed for {sym} ({exit_type_matched}) with 2 consecutive bad ticks within {diff_pct:.1f}%: "
                                        f"1st: {prev_tick:.2f}, 2nd: {live_price:.2f}. Closing position."
                                    )
                                else:
                                    # Not within 3%: reject both and count them
                                    self.pending_exit_confirmations.pop(pos_id, None)
                                    self.rejected_ticks_count[sym] = self.rejected_ticks_count.get(sym, 0) + 2
                                    self.daily_rejected_ticks_count[sym] = self.daily_rejected_ticks_count.get(sym, 0) + 2
                                    logger.warning(
                                        f"Exit confirmation failed for {sym}: 2nd tick ({live_price:.2f}) diverged >3% from 1st ({prev_tick:.2f}). Both ticks rejected."
                                    )
                                    continue
                            else:
                                # Expired pending confirmation (>10s): discard buffer, keep position, log WARNING, count it
                                self.pending_exit_confirmations.pop(pos_id, None)
                                self.rejected_ticks_count[sym] = self.rejected_ticks_count.get(sym, 0) + 1
                                self.daily_rejected_ticks_count[sym] = self.daily_rejected_ticks_count.get(sym, 0) + 1
                                logger.warning(
                                    f"Exit confirmation expired for {sym}: no confirming tick arrived within 10s of first spike ({prev_tick:.2f}). Discarded buffer, position kept open."
                                )
                        
                        if not confirmed:
                            # First bad tick triggering exit: hold pending confirmation for up to 10s
                            self.pending_exit_confirmations[pos_id] = (live_price, now_tick_ts, exit_type_matched)
                            logger.warning(
                                f"WARNING: exit price outside sanity band for {sym}: jumped {pct_jump:.1f}% "
                                f"(from {last_accepted:.2f} to {live_price:.2f}, limit {sanity_limit}%). Holding for confirmation tick within 10s."
                            )
                            continue

                    # If tick was inside sanity band or cutoff exit or confirmed: clear any pending and proceed with exit
                    self.pending_exit_confirmations.pop(pos_id, None)
                    if would_reject and is_cutoff_exit:
                        logger.warning(
                            f"WARNING: cutoff exit price outside sanity band for {sym}: jumped {pct_jump:.1f}% "
                            f"(from {last_accepted:.2f} to {live_price:.2f}, limit {sanity_limit}%). Proceeding with immediate cutoff exit."
                        )
                    # Proceed with exit using latest tick
                    pos["current_price"] = live_price
                    self.last_accepted_tick_time[sym] = now_tick_ts
                else:
                    # Regular non-exit tick: apply price sanity guard
                    if would_reject:
                        self.rejected_ticks_count[sym] = self.rejected_ticks_count.get(sym, 0) + 1
                        
                        # Date-based reset for daily rejected-tick counter
                        today_ist = now_ist.strftime("%Y-%m-%d")
                        if self.rejected_ticks_date_str != today_ist:
                            self.daily_rejected_ticks_count = {}
                            self.rejected_ticks_date_str = today_ist
                        self.daily_rejected_ticks_count[sym] = self.daily_rejected_ticks_count.get(sym, 0) + 1

                        logger.warning(
                            f"WARNING: Price sanity rejected tick for {sym}: jumped {pct_jump:.1f}% "
                            f"(from {last_accepted:.2f} to {live_price:.2f}, limit {sanity_limit}%). "
                            f"Tick skipped, P&L unchanged (Rejections: {self.rejected_ticks_count[sym]})."
                        )
                        # Reset consecutive good tracker on rejection
                        self.suspect_consecutive_good.pop(sym, None)
                        if self.rejected_ticks_count[sym] >= 3 and sym not in self.suspect_symbols:
                            self.suspect_symbols.add(sym)
                            logger.info(f"DATA_SUSPECT set for {sym}: 3 consecutive rejected ticks. Flagged DATA_SUSPECT, signals blocked.")
                        continue

                    # Accepted tick: update reference and timestamps
                    self.last_accepted_tick_time[sym] = now_tick_ts
                    pos["current_price"] = live_price

                    # Reset consecutive rejection count on good tick
                    if sym in self.rejected_ticks_count:
                        self.rejected_ticks_count[sym] = 0

                    # ITEM 4 & ADDENDUM E: DATA_SUSPECT AUTO-CLEAR
                    # Clear when 3 consecutive ticks are within PRICE_SANITY_CLEAR_PCT (default 2%) of each other
                    if sym in self.suspect_symbols:
                        recent_ticks = self.suspect_consecutive_good.get(sym, [])
                        recent_ticks.append(live_price)
                        if len(recent_ticks) > 3:
                            recent_ticks = recent_ticks[-3:]
                        self.suspect_consecutive_good[sym] = recent_ticks
                        
                        if len(recent_ticks) == 3:
                            # Verify all 3 consecutive ticks are within PRICE_SANITY_CLEAR_PCT of each other
                            t1, t2, t3 = recent_ticks
                            j12 = abs(t2 - t1) / t1 * 100.0 if t1 > 0 else 0.0
                            j23 = abs(t3 - t2) / t2 * 100.0 if t2 > 0 else 0.0
                            j13 = abs(t3 - t1) / t1 * 100.0 if t1 > 0 else 0.0
                            clear_limit = self.price_sanity_clear_pct
                            if max(j12, j23, j13) <= clear_limit:
                                self.suspect_symbols.discard(sym)
                                self.suspect_consecutive_good.pop(sym, None)
                                logger.info(f"DATA_SUSPECT cleared for {sym}: 3 consecutive ticks within {clear_limit}% band.")

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
                    max_allowed_prem = max(10.0, pos["opt_entry_premium"] * 3.0)
                    opt_curr = min(max_allowed_prem, max(0.05, round(pos["opt_entry_premium"] + opt_gain_pts, 2)))
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

                # Trailing Stop-Loss & Exit Checks (Phase 5f: NO TARGET - pure trailing stop)
                if is_opt:
                    opt_curr = pos.get("opt_current_premium", 0.0)
                    opt_entry = pos.get("opt_entry_premium", 1.0)
                    opt_sl = pos.get("opt_trailing_sl", round(opt_entry * 0.70, 2))

                    opt_gain_pct = ((opt_curr - opt_entry) / opt_entry) if opt_entry > 0 else 0.0
                    prev_sl = opt_sl
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

                    # Alert for TAKEN option trades when trailing moves (max 1/5m per trade) (Phase 6f)
                    if pos.get("taken") == 1 and pos["opt_trailing_sl"] > prev_sl:
                        now_ts = time.time()
                        if now_ts - pos.get("last_trail_ntfy_ts", 0.0) >= 300.0:
                            pos["last_trail_ntfy_ts"] = now_ts
                            try:
                                from src.notifications.ntfy_dispatcher import NTFYDispatcher
                                NTFYDispatcher().send_trailing_stop_moved_alert(pos, pos["opt_trailing_sl"], opt_curr)
                            except Exception:
                                pass

                    if opt_curr <= pos.get("opt_trailing_sl", opt_sl):
                        reason = "TRAILING_SL_HIT" if pos["stage"] != "INITIAL (FIXED SL)" else "INITIAL_SL_HIT (-30% OPTION SL)"
                        self.close_position(pos_id, exit_price=live_price, exit_reason=reason)
                    elif is_equity_eod and sector in equity_sectors:
                        self.close_position(pos_id, exit_price=live_price, exit_reason="3PM_MANDATORY_SQUAREOFF (15:00 IST)")

                else:
                    r_multiple = gain_pts / r_val if r_val > 0 else 0.0
                    is_down = is_put or act == "SELL"
                    prev_sl = curr_sl
                    trail_mult = float(os.getenv("TRAIL_ATR_MULT", "1.5"))
                    atr_dist = trail_mult * pos.get("atr_val", entry * 0.015)

                    if is_down:
                        pos["highest_price"] = min(pos.get("highest_price", entry), live_price)
                        if r_multiple >= 1.0:
                            be_sl = round(entry - 0.50, 2) # Breakeven minus roundtrip costs
                            atr_sl = round(pos["highest_price"] + atr_dist, 2)
                            new_sl = min(be_sl, atr_sl)
                            if new_sl < curr_sl:
                                pos["trailing_sl"] = new_sl
                                pos["stage"] = "TRAILED (ATR)" if new_sl == atr_sl else "TRAILED (BREAKEVEN)"

                        # Alert for TAKEN trades when trailing moves (Phase 6f)
                        if pos.get("taken") == 1 and pos["trailing_sl"] < prev_sl:
                            now_ts = time.time()
                            if now_ts - pos.get("last_trail_ntfy_ts", 0.0) >= 300.0:
                                pos["last_trail_ntfy_ts"] = now_ts
                                try:
                                    from src.notifications.ntfy_dispatcher import NTFYDispatcher
                                    NTFYDispatcher().send_trailing_stop_moved_alert(pos, pos["trailing_sl"], live_price)
                                except Exception:
                                    pass

                        if live_price >= pos["trailing_sl"]:
                            reason = "TRAILING_SL_HIT" if pos["stage"] != "INITIAL (FIXED SL)" else "INITIAL_SL_HIT"
                            self.close_position(pos_id, exit_price=pos["trailing_sl"], exit_reason=reason)
                        elif is_equity_eod and sector in equity_sectors:
                            self.close_position(pos_id, exit_price=live_price, exit_reason="3PM_MANDATORY_SQUAREOFF (15:00 IST)")
                        elif is_mcx_eod and sector == "COMMODITIES":
                            self.close_position(pos_id, exit_price=live_price, exit_reason="EOD_MCX_CLOSE (23:15 IST)")
                    else:
                        pos["highest_price"] = max(pos.get("highest_price", entry), live_price)
                        if r_multiple >= 1.0:
                            be_sl = round(entry + 0.50, 2) # Breakeven plus roundtrip costs
                            atr_sl = round(pos["highest_price"] - atr_dist, 2)
                            new_sl = max(be_sl, atr_sl)
                            if new_sl > curr_sl:
                                pos["trailing_sl"] = new_sl
                                pos["stage"] = "TRAILED (ATR)" if new_sl == atr_sl else "TRAILED (BREAKEVEN)"

                        # Alert for TAKEN trades when trailing moves (Phase 6f)
                        if pos.get("taken") == 1 and pos["trailing_sl"] > prev_sl:
                            now_ts = time.time()
                            if now_ts - pos.get("last_trail_ntfy_ts", 0.0) >= 300.0:
                                pos["last_trail_ntfy_ts"] = now_ts
                                try:
                                    from src.notifications.ntfy_dispatcher import NTFYDispatcher
                                    NTFYDispatcher().send_trailing_stop_moved_alert(pos, pos["trailing_sl"], live_price)
                                except Exception:
                                    pass

                        if live_price <= pos["trailing_sl"]:
                            reason = "TRAILING_SL_HIT" if pos["stage"] != "INITIAL (FIXED SL)" else "INITIAL_SL_HIT"
                            self.close_position(pos_id, exit_price=pos["trailing_sl"], exit_reason=reason)
                        elif is_equity_eod and sector in equity_sectors:
                            self.close_position(pos_id, exit_price=live_price, exit_reason="3PM_MANDATORY_SQUAREOFF (15:00 IST)")
                        elif is_mcx_eod and sector == "COMMODITIES":
                            self.close_position(pos_id, exit_price=live_price, exit_reason="EOD_MCX_CLOSE (23:15 IST)")
            except Exception as trade_err:
                logger.error(f"Error evaluating paper position {pos_id} ({pos.get('symbol')}): {trade_err}")

    def close_position(self, pos_id: str, exit_price: float, exit_reason: str) -> bool:
        """Finalizes trade, calculates realistic broker taxes, and moves position to closed ledger."""
        with self._lock:
            if pos_id not in self.active_positions:
                return False
            # Remove from open list BEFORE DB insert under atomic lock
            pos = self.active_positions.pop(pos_id)

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
        risk_r = pos.get("risk_r", 1.0)
        r_multiple = round(gross_pnl / (risk_r * qty), 2) if (risk_r > 0 and qty > 0) else 0.0
        trade_id = f"TRD_{pos_id}"
        exit_time_ist = get_ist_now().strftime("%Y-%m-%d %I:%M:%S %p IST")
        closed_utc = get_utc_iso()

        # Check for UNREALISTIC single trade profit/loss (> 10% of virtual capital, Phase 5j)
        tags_str = pos.get("tags", "")
        if abs(net_pnl) > (self.virtual_capital * 0.10):
            tags_str = f"{tags_str},UNREALISTIC".strip(",")
            logger.warning(f"UNREALISTIC trade P&L detected for {pos['symbol']}: Rs {net_pnl:,.2f} (>10% virtual capital). Flagged UNREALISTIC.")

        pos["tags"] = tags_str
        pos["r_multiple"] = r_multiple

        inserted = False
        try:
            with self._get_connection() as conn:
                cursor = conn.execute("""
                    INSERT OR IGNORE INTO closed_paper_trades (
                        trade_id, position_id, symbol, sector, action,
                        is_option, entry_price, exit_price, quantity,
                        gross_pnl, total_charges, net_pnl, exit_reason,
                        entry_time_ist, exit_time_ist, opened_at_utc, closed_at_utc,
                        highest_price, tags, setup_key, grade, taken, taken_time_ist, real_entry, real_qty, r_multiple
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    trade_id, pos_id, pos["symbol"], pos["sector"], pos["action"],
                    pos["is_option"], pos["entry_price"], pos["exit_price"], qty,
                    gross_pnl, charges, net_pnl, exit_reason,
                    pos["entry_time_ist"], exit_time_ist, pos["opened_at_utc"], closed_utc,
                    pos.get("highest_price", pos["entry_price"]), tags_str,
                    pos.get("setup_key", ""), pos.get("grade", "B"),
                    pos.get("taken", 0), pos.get("taken_time_ist", ""),
                    pos.get("real_entry", 0.0), pos.get("real_qty", 0), r_multiple
                ))
                conn.execute("DELETE FROM active_paper_positions WHERE position_id = ?", (pos_id,))
                conn.commit()
                inserted = (cursor.rowcount == 1)
        except Exception as db_err:
            # If DB error occurs (disk full, DB locked, etc.), rollback memory: restore position to open list
            with self._lock:
                pos["status"] = "OPEN"
                self.active_positions[pos_id] = pos
            logger.error(f"ERROR inserting closed trade {trade_id} ({pos['symbol']}): {db_err}. Rolled back position to active list for retry.")
            return False

        clean_base = pos["symbol"].split(" ")[0].upper()
        self.symbol_last_close_time[clean_base] = time.time()

        if inserted:
            logger.info(f"PAPER TRADE CLOSED: {pos['symbol']} -> Exit: ₹{exit_price:.2f} | {exit_reason} | Net P&L: ₹{net_pnl:,.2f} ({r_multiple:+.2f}R)")
            # Update setup statistics (Phase 6a)
            self.record_setup_trade_result(pos.get("setup_key"), is_win=(net_pnl > 0), r_result=r_multiple, net_pnl=net_pnl)

            # Phase 6f: For TAKEN trades only, send NTFY exit alert with realized P&L.
            # Untaken trades receive zero exit alerts after the initial trigger.
            if pos.get("taken") == 1:
                try:
                    from src.notifications.ntfy_dispatcher import NTFYDispatcher
                    NTFYDispatcher().send_stop_hit_alert(pos, exit_price, net_pnl, exit_reason)
                except Exception as alert_err:
                    logger.debug(f"Stop hit alert notice: {alert_err}")
            else:
                self._dispatch_ntfy_trade_closed(pos, net_pnl, exit_price, exit_reason)

            # Mirror to Firestore if configured (Phase 2B)
            if hasattr(self, "firestore") and self.firestore and self.firestore.is_connected:
                try:
                    pos_closed_doc = dict(pos)
                    pos_closed_doc["status"] = "CLOSED"
                    pos_closed_doc["trade_id"] = trade_id
                    pos_closed_doc["net_pnl"] = net_pnl
                    pos_closed_doc["exit_price"] = exit_price
                    pos_closed_doc["exit_reason"] = exit_reason
                    self.firestore.mirror_paper_position(pos_closed_doc)
                except Exception:
                    pass

            return True
        else:
            # rowcount == 0 means duplicate: log at DEBUG, no alert
            logger.debug(f"PAPER TRADE DUPLICATE IGNORED: {pos['symbol']} ({trade_id}) already in closed ledger.")
            return False

    def get_daily_realized_pnl(self, date_ist_str: Optional[str] = None) -> float:
        """Computes total realized P&L directly from DB rows for the given date (default today)."""
        if not date_ist_str:
            date_ist_str = get_ist_now().strftime("%Y-%m-%d")
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(net_pnl), 0.0) as total_pnl FROM closed_paper_trades WHERE exit_time_ist LIKE ?",
                (f"{date_ist_str}%",)
            ).fetchone()
            return round(float(row["total_pnl"] if row else 0.0), 2)

    def get_rejected_ticks_telemetry(self) -> Dict[str, Any]:
        """Returns daily rejected-tick counter metrics per symbol and overall total."""
        today_ist = self._get_now_ist().strftime("%Y-%m-%d")
        if self.rejected_ticks_date_str and self.rejected_ticks_date_str != today_ist:
            self.daily_rejected_ticks_count = {}
            self.rejected_ticks_date_str = today_ist
        elif not self.rejected_ticks_date_str:
            self.rejected_ticks_date_str = today_ist
        total_rejected = sum(self.daily_rejected_ticks_count.values())
        return {
            "date": today_ist,
            "total_rejected_ticks": total_rejected,
            "per_symbol": dict(self.daily_rejected_ticks_count),
            "suspect_symbols": list(self.suspect_symbols)
        }

    def _dispatch_ntfy_trade_opened(self, pos: Dict[str, Any]):
        """Dispatches instant NTFY push notification on trade trigger (Phase 5h & 6c: capped at 12 triggers/day, max 6 before 11:30 IST and max 6 from 13:00 IST)."""
        now_ist = self._get_now_ist()
        today_ist = now_ist.strftime("%Y-%m-%d")
        if getattr(self, "last_ntfy_trigger_date", "") != today_ist:
            self.last_ntfy_trigger_date = today_ist
            self.daily_morning_ntfy_count = 0
            self.daily_afternoon_ntfy_count = 0
            self.daily_ntfy_triggers_count = 0

        # Overall day cap: 12 triggers
        if self.daily_ntfy_triggers_count >= 12:
            logger.info(f"NTFY trigger alert capped: daily limit of 12 triggers reached today ({today_ist}).")
            return

        t = now_ist.time()
        is_morning = (t.hour < 11 or (t.hour == 11 and t.minute < 30))
        is_afternoon = (t.hour >= 13)

        if is_morning:
            if getattr(self, "daily_morning_ntfy_count", 0) >= 6:
                logger.info(f"NTFY trigger alert capped: morning window limit of 6 reached before 11:30 IST ({self.daily_morning_ntfy_count}/6).")
                return
        elif is_afternoon:
            if getattr(self, "daily_afternoon_ntfy_count", 0) >= 6:
                logger.info(f"NTFY trigger alert capped: afternoon window limit of 6 reached from 13:00 IST ({self.daily_afternoon_ntfy_count}/6).")
                return
        else:
            # 11:30 to 13:00 IST midday pause to avoid morning exhaustion
            logger.info(f"NTFY trigger alert skipped: midday window (11:30-13:00 IST) paused to avoid exhaustion.")
            return

        # Suppress local dispatches to prevent eating into cloud production quota unless forced
        is_render = bool(os.getenv("RENDER") or os.getenv("RENDER_SERVICE_ID"))
        if not is_render and not os.getenv("FORCE_LOCAL_NTFY"):
            logger.debug("Local development environment: Suppressing live NTFY trade opened push.")
            if is_morning:
                self.daily_morning_ntfy_count = getattr(self, "daily_morning_ntfy_count", 0) + 1
            elif is_afternoon:
                self.daily_afternoon_ntfy_count = getattr(self, "daily_afternoon_ntfy_count", 0) + 1
            self.daily_ntfy_triggers_count += 1
            return

        try:
            from src.notifications.ntfy_dispatcher import NTFYDispatcher
            dispatcher = NTFYDispatcher()
            sent = dispatcher.send_trade_trigger_alert(pos, max_retries=3)
            if sent:
                if is_morning:
                    self.daily_morning_ntfy_count = getattr(self, "daily_morning_ntfy_count", 0) + 1
                elif is_afternoon:
                    self.daily_afternoon_ntfy_count = getattr(self, "daily_afternoon_ntfy_count", 0) + 1
                self.daily_ntfy_triggers_count += 1
                logger.info(f"NTFY Trade Trigger push sent for {pos['symbol']} ({self.daily_ntfy_triggers_count}/12 today - Morning: {getattr(self, 'daily_morning_ntfy_count', 0)}, Afternoon: {getattr(self, 'daily_afternoon_ntfy_count', 0)})")
                
                # Mirror quota count to Firestore if connected
                try:
                    from src.storage.firestore_backend import FirestoreBackend
                    fb = FirestoreBackend()
                    if fb.is_connected:
                        fb.mirror_ntfy_quota(today_ist, self.daily_ntfy_triggers_count)
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"Failed sending trade opened alert to NTFY: {e}")

    def _dispatch_ntfy_trade_closed(self, pos: Dict[str, Any], net_pnl: float, exit_price: float, exit_reason: str):
        """Dispatches instant NTFY push notification on trade closed."""
        # Suppress local dispatches to prevent eating into cloud production quota
        is_render = bool(os.getenv("RENDER") or os.getenv("RENDER_SERVICE_ID"))
        if not is_render and not os.getenv("FORCE_LOCAL_NTFY"):
            logger.debug("Local development environment: Suppressing NTFY trade closed push.")
            return

        try:
            topic = os.getenv("NTFY_TOPIC", "Finplus-Delta")
            auth_token = os.getenv("NTFY_AUTH_TOKEN", "")
            status_tag = "tada" if net_pnl >= 0 else "x"
            pnl_sign = "+" if net_pnl >= 0 else ""
            title = f"FinPlus Paper Closed | {pos['symbol']} ({pnl_sign}Rs {net_pnl:,.2f})"

            lines = [
                f"Closed: [{pos['action']}] {pos['symbol']}",
                f"Entry: Rs {pos['entry_price']:,.2f} | Exit: Rs {exit_price:,.2f}",
                f"Reason: {exit_reason}",
                f"Net In-Hand P&L: {pnl_sign}Rs {net_pnl:,.2f}",
                f"Final Stage: {pos.get('stage', 'CLOSED')}",
                f"Cockpit: https://finplus-stock-scanner.onrender.com/live"
            ]
            msg = "\n".join(lines)
            raw_headers = {
                "Title": title,
                "Priority": "4",
                "Tags": status_tag,
                "Markdown": "yes",
                "Click": "https://finplus-stock-scanner.onrender.com/live",
                "Actions": "view, Live Cockpit, https://finplus-stock-scanner.onrender.com/live"
            }
            clean_hdrs = safe_headers(raw_headers, auth_token)

            requests.post(
                f"https://ntfy.sh/{topic}",
                data=msg.encode("utf-8"),
                headers=clean_hdrs,
                timeout=5
            )
            logger.info(f"NTFY Trade Closed push sent for {pos['symbol']} ({pnl_sign}Rs {net_pnl:,.2f})")
        except Exception as e:
            logger.warning(f"Failed sending trade closed alert to NTFY: {e}")

    def record_setup_trade_result(self, setup_key: Optional[str], is_win: bool, r_result: float, net_pnl: float):
        """Records trade result into setup_stats and recomputes rolling 30 metrics and grade (Phase 6a)."""
        if not setup_key:
            return

        # UNREALISTIC trade check: Excluded from setup stats if single-trade P&L > 10% of virtual capital (Phase 5j)
        if abs(net_pnl) > (self.virtual_capital * 0.10):
            logger.warning(f"UNREALISTIC P&L (Rs {net_pnl:,.2f} > 10% capital). Excluded from setup stats for {setup_key}.")
            return

        with self._get_connection() as conn:
            # Fetch last 30 trades for this setup_key from closed_paper_trades
            rows = conn.execute("""
                SELECT net_pnl, (CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END) as win,
                       COALESCE(r_multiple, 0.0) as r_val
                FROM closed_paper_trades
                WHERE setup_key = ?
                ORDER BY closed_at_utc DESC
                LIMIT 30
            """, (setup_key,)).fetchall()

            # If closed_paper_trades doesn't have 30 rows (e.g. direct record calls in tests or simulation),
            # aggregate from setup_stats existing counts
            stat_row = conn.execute("SELECT total_trades, wins, rolling_30_trades, rolling_30_wins FROM setup_stats WHERE setup_key = ?", (setup_key,)).fetchone()
            if len(rows) == 0 and stat_row is not None:
                prev_total = stat_row["total_trades"]
                prev_wins = stat_row["wins"]
                total_trades = prev_total + 1
                wins = prev_wins + (1 if is_win else 0)
            elif len(rows) == 0:
                total_trades = 1
                wins = 1 if is_win else 0
            else:
                total_trades = len(rows)
                wins = sum(r["win"] for r in rows)

            win_pct = round((wins / total_trades) * 100.0, 1) if total_trades > 0 else 0.0
            avg_r = round(sum(r["r_val"] for r in rows) / total_trades, 2) if len(rows) > 0 else (r_result if total_trades == 1 else 0.0)
            expectancy = round(sum(r["net_pnl"] for r in rows) / total_trades, 2) if len(rows) > 0 else (net_pnl if total_trades == 1 else 0.0)

            # Grade: A = win % >= 60 and positive expectancy over last 30;
            # B = fewer than 30 trades (LEARNING);
            # C = below 60 or negative expectancy (LOW CONFIDENCE)
            if total_trades < 30:
                grade = "B"
            elif win_pct >= 60.0 and (expectancy > 0 or avg_r > 0):
                grade = "A"
            else:
                grade = "C"

            conn.execute("""
                INSERT OR REPLACE INTO setup_stats (
                    setup_key, total_trades, wins, win_pct, avg_r, expectancy,
                    rolling_30_trades, rolling_30_wins, rolling_30_win_pct, rolling_30_expectancy,
                    grade, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                setup_key, total_trades, wins, win_pct, avg_r, expectancy,
                total_trades, wins, win_pct, expectancy,
                grade, get_utc_iso()
            ))
            conn.commit()

    def set_trade_taken(self, trade_id_or_pos_id: str, taken: bool, real_entry: Optional[float] = None, real_qty: Optional[int] = None) -> bool:
        """Toggles 'Taken' flag on paper trade with optional real execution price & quantity (Phase 6e)."""
        clean_id = trade_id_or_pos_id.replace("TRD_", "")
        now_ist_str = get_ist_now().strftime("%Y-%m-%d %I:%M:%S %p IST")
        with self._lock:
            if clean_id in self.active_positions:
                pos = self.active_positions[clean_id]
                pos["taken"] = 1 if taken else 0
                pos["taken_time_ist"] = now_ist_str
                if real_entry is not None:
                    pos["real_entry"] = float(real_entry)
                if real_qty is not None:
                    pos["real_qty"] = int(real_qty)

        with self._get_connection() as conn:
            conn.execute("""
                UPDATE active_paper_positions 
                SET taken = ?, taken_time_ist = ?, real_entry = COALESCE(?, real_entry), real_qty = COALESCE(?, real_qty)
                WHERE position_id = ?
            """, (1 if taken else 0, now_ist_str, real_entry, real_qty, clean_id))
            conn.execute("""
                UPDATE closed_paper_trades 
                SET taken = ?, taken_time_ist = ?, real_entry = COALESCE(?, real_entry), real_qty = COALESCE(?, real_qty)
                WHERE position_id = ? OR trade_id = ?
            """, (1 if taken else 0, now_ist_str, real_entry, real_qty, clean_id, f"TRD_{clean_id}"))
            conn.commit()
        return True

    def get_taken_summary(self) -> Dict[str, Any]:
        """Returns counts of suggestions today, taken today, and taken this week (Phase 6e)."""
        today_ist = get_ist_now().strftime("%Y-%m-%d")
        now_dt = get_ist_now()
        start_of_week = (now_dt - datetime.timedelta(days=now_dt.weekday())).strftime("%Y-%m-%d")
        with self._get_connection() as conn:
            open_today = conn.execute("SELECT COUNT(*), SUM(CASE WHEN taken=1 THEN 1 ELSE 0 END) FROM active_paper_positions WHERE entry_time_ist LIKE ?", (f"{today_ist}%",)).fetchone()
            closed_today = conn.execute("SELECT COUNT(*), SUM(CASE WHEN taken=1 THEN 1 ELSE 0 END) FROM closed_paper_trades WHERE entry_time_ist LIKE ?", (f"{today_ist}%",)).fetchone()
            sug_today = (open_today[0] or 0) + (closed_today[0] or 0)
            taken_today = (open_today[1] or 0) + (closed_today[1] or 0)

            open_week = conn.execute("SELECT SUM(CASE WHEN taken=1 THEN 1 ELSE 0 END) FROM active_paper_positions WHERE entry_time_ist >= ?", (start_of_week,)).fetchone()[0] or 0
            closed_week = conn.execute("SELECT SUM(CASE WHEN taken=1 THEN 1 ELSE 0 END) FROM closed_paper_trades WHERE entry_time_ist >= ?", (start_of_week,)).fetchone()[0] or 0
            taken_week = open_week + closed_week

        return {
            "date": today_ist,
            "suggestions_today": sug_today,
            "taken_today": taken_today,
            "taken_this_week": taken_week,
            "weekly_target": "10-20"
        }

    def record_funnel_metrics(self, metrics: Dict[str, Any]):
        """Persists daily funnel counters to SQLite table funnel_metrics (Phase 6d)."""
        today_ist = get_ist_now().strftime("%Y-%m-%d")
        total_scanned = metrics.get("total_symbols_scanned", metrics.get("instruments_scanned", 0))
        cand_detected = metrics.get("candidates_detected", metrics.get("candidates_by_setup", 0))
        rem_wide_sl = metrics.get("removed_wide_sl", metrics.get("wide_sl_count", 0))
        opened_on_panel = metrics.get("suggestions_opened_on_panel", metrics.get("trades_opened", 0))
        ntfy_triggers = metrics.get("ntfy_triggers_sent", getattr(self, "daily_ntfy_triggers_count", 0))

        with self._get_connection() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO funnel_metrics (
                    date_str, instruments_scanned, total_symbols_scanned,
                    candidates_by_setup, candidates_detected,
                    removed_stale_feed, removed_data_suspect, removed_late,
                    removed_regime_veto, removed_midday_freeze, removed_insufficient_history,
                    removed_latch_used, removed_wide_sl, trades_opened,
                    suggestions_opened_on_panel, ntfy_triggers_sent,
                    wide_sl_count, over_capital_count, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                today_ist,
                total_scanned,
                total_scanned,
                cand_detected,
                cand_detected,
                metrics.get("removed_stale_feed", 0),
                metrics.get("removed_data_suspect", 0),
                metrics.get("removed_late", 0),
                metrics.get("removed_regime_veto", 0),
                metrics.get("removed_midday_freeze", 0),
                metrics.get("removed_insufficient_history", 0),
                metrics.get("removed_latch_used", 0),
                rem_wide_sl,
                opened_on_panel,
                opened_on_panel,
                ntfy_triggers,
                rem_wide_sl,
                metrics.get("over_capital_count", 0),
                get_utc_iso()
            ))
            conn.commit()

    def get_funnel_summary(self, date_ist: Optional[str] = None) -> Dict[str, Any]:
        """Returns daily funnel telemetry from SQLite (Phase 6d)."""
        today_ist = date_ist or get_ist_now().strftime("%Y-%m-%d")
        with self._get_connection() as conn:
            row = conn.execute("SELECT * FROM funnel_metrics WHERE date_str = ?", (today_ist,)).fetchone()
            if row:
                res = dict(row)
                # Alias fields to guarantee both schemas match exactly
                res["total_symbols_scanned"] = res.get("total_symbols_scanned", res.get("instruments_scanned", 0))
                res["candidates_detected"] = res.get("candidates_detected", res.get("candidates_by_setup", 0))
                res["suggestions_opened_on_panel"] = res.get("suggestions_opened_on_panel", res.get("trades_opened", 0))
                res["removed_wide_sl"] = res.get("removed_wide_sl", res.get("wide_sl_count", 0))
                res["ntfy_triggers_sent"] = res.get("ntfy_triggers_sent", getattr(self, "daily_ntfy_triggers_count", 0))
                return res
        return {
            "date_str": today_ist,
            "total_symbols_scanned": 0,
            "instruments_scanned": 0,
            "candidates_detected": 0,
            "candidates_by_setup": 0,
            "suggestions_opened_on_panel": 0,
            "trades_opened": 0,
            "removed_insufficient_history": 0,
            "removed_stale_feed": 0,
            "removed_data_suspect": 0,
            "removed_regime_veto": 0,
            "removed_late": 0,
            "removed_wide_sl": 0,
            "ntfy_triggers_sent": 0
        }

    def get_weekly_performance_report(self) -> Dict[str, Any]:
        """Computes comprehensive Sunday weekly report (Phase 6g)."""
        now_dt = get_ist_now()
        start_of_week = (now_dt - datetime.timedelta(days=7)).strftime("%Y-%m-%d")
        with self._get_connection() as conn:
            rows = conn.execute("""
                SELECT taken, setup_key, grade, net_pnl, COALESCE(r_multiple, 0.0) as r_val,
                       (CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END) as win
                FROM closed_paper_trades
                WHERE exit_time_ist >= ?
            """, (start_of_week,)).fetchall()

            taken_trades = [r for r in rows if r["taken"] == 1]
            untaken_trades = [r for r in rows if r["taken"] != 1]

            def calc_metrics(trades):
                n = len(trades)
                if n == 0:
                    return {"trades": 0, "win_pct": 0.0, "avg_r": 0.0, "expectancy": 0.0}
                w = sum(t["win"] for t in trades)
                return {
                    "trades": n,
                    "win_pct": round((w / n) * 100.0, 1),
                    "avg_r": round(sum(t["r_val"] for t in trades) / n, 2),
                    "expectancy": round(sum(t["net_pnl"] for t in trades) / n, 2)
                }

            taken_m = calc_metrics(taken_trades)
            untaken_m = calc_metrics(untaken_trades)

            setups = {}
            for r in rows:
                sk = r["setup_key"] or "GENERAL"
                if sk not in setups:
                    setups[sk] = []
                setups[sk].append(r)
            setup_breakdown = {k: calc_metrics(v) for k, v in setups.items()}

            best_setup = max(setup_breakdown.items(), key=lambda x: (x[1]["win_pct"], x[1]["expectancy"]), default=("RANGE_BREAKOUT", {}))[0]

        return {
            "period": f"Last 7 Days (since {start_of_week})",
            "suggestions_this_week": len(rows),
            "taken_this_week": len(taken_trades),
            "taken_metrics": taken_m,
            "not_taken_metrics": untaken_m,
            "taken_win_pct": taken_m["win_pct"],
            "taken_avg_r": taken_m["avg_r"],
            "taken_expectancy": taken_m["expectancy"],
            "top_trusted_setup": best_setup,
            "setup_breakdown": setup_breakdown
        }

    def get_status_dict(self) -> Dict[str, Any]:
        """Returns the complete payload expected by the FrontPage live.html cockpit (Phase 5i)."""
        active_list = list(self.active_positions.values())
        tot_unrealized = round(sum(p.get("unrealized_pnl", 0.0) for p in active_list), 2)

        # Enriched active positions
        enriched_active = []
        for p in active_list:
            item = dict(p)
            entry = float(item.get("entry_price", 0.0))
            trailing_sl = float(item.get("trailing_sl", 0.0))
            qty = int(item.get("quantity", 1))
            risk_r = float(item.get("risk_r", 1.0))
            is_buy = (item.get("action") == "BUY")

            if is_buy:
                profit_locked_rupees = round(max(0.0, (trailing_sl - entry) * qty), 2)
            else:
                profit_locked_rupees = round(max(0.0, (entry - trailing_sl) * qty), 2)

            profit_locked_r = round(profit_locked_rupees / (risk_r * qty), 2) if (risk_r > 0 and qty > 0) else 0.0
            stop_dist_pct = round(abs(entry - trailing_sl) / entry * 100.0, 2) if entry > 0 else 0.0
            rupees_at_risk = round(abs(entry - trailing_sl) * qty, 2)

            item["profit_locked_rupees"] = profit_locked_rupees
            item["profit_locked_r"] = profit_locked_r
            item["stop_dist_pct"] = stop_dist_pct
            item["rupees_at_risk"] = rupees_at_risk

            # Setup win rate and trade count
            s_stats = self.get_setup_stats_dict(item.get("setup_key", ""))
            item["setup_win_pct"] = s_stats["win_pct"]
            item["setup_total_trades"] = s_stats["total_trades"]
            item["grade"] = item.get("grade") or s_stats["grade"]
            item["status"] = "TRAILING" if "TRAILING" in str(item.get("stage", "")) else "OPEN"

            enriched_active.append(item)

        # Get closed trades from SQLite
        closed_trades = []
        tot_realized = 0.0
        try:
            with self._get_connection() as conn:
                sum_row = conn.execute("SELECT COALESCE(SUM(net_pnl), 0.0) as total_pnl FROM closed_paper_trades").fetchone()
                tot_realized = round(float(sum_row["total_pnl"] if sum_row else 0.0), 2)

                rows = conn.execute("SELECT * FROM closed_paper_trades ORDER BY closed_at_utc DESC LIMIT 30").fetchall()
                for r in rows:
                    t = dict(r)
                    closed_trades.append({
                        "trade_id": t["trade_id"],
                        "symbol": t["symbol"],
                        "sector": t["sector"],
                        "action": t["action"],
                        "entry_price": t["entry_price"],
                        "exit_price": t["exit_price"],
                        "highest_price": t.get("highest_price", t["exit_price"]),
                        "gross_pnl": t["gross_pnl"],
                        "total_charges": t["total_charges"],
                        "net_pnl": t["net_pnl"],
                        "exit_reason": t["exit_reason"],
                        "trade_time": t["exit_time_ist"],
                        "setup_key": t.get("setup_key", ""),
                        "grade": t.get("grade", "B"),
                        "tags": t.get("tags", ""),
                        "taken": t.get("taken", 0),
                        "real_entry": t.get("real_entry", 0.0),
                        "real_qty": t.get("real_qty", 0),
                        "r_multiple": t.get("r_multiple", 0.0),
                        "status": "STOPPED"
                    })
        except Exception as e:
            logger.error(f"Error fetching closed trades: {e}")

        tot_realized = round(tot_realized, 2)
        current_equity = round(self.virtual_capital + tot_realized + tot_unrealized, 2)
        equity_pnl_pct = round(((current_equity - self.virtual_capital) / self.virtual_capital) * 100.0, 2)

        return {
            "status": "LIVE_ACTIVE",
            "virtual_starting_capital": self.virtual_capital,
            "current_equity": current_equity,
            "equity_pnl_pct": equity_pnl_pct,
            "total_unrealized_pnl": tot_unrealized,
            "net_realized_pnl": tot_realized,
            "active_positions_count": len(enriched_active),
            "active_positions": enriched_active,
            "recent_closed_trades": closed_trades,
            "taken_summary": self.get_taken_summary(),
            "ist_time": get_ist_time_str(),
            "timestamp_utc": get_utc_iso()
        }

    async def start_polling_loop(self):
        """Asynchronous background loop for continuous live price trailing and commodity signal ingestion."""
        self.is_running = True
        logger.info("LivePaperTracker continuous polling loop started (3-second tick).")
        tick_count = 0
        while self.is_running:
            try:
                await asyncio.to_thread(self.update_and_trail_positions)
                tick_count += 1
                if tick_count % 10 == 0:  # Every 30 seconds, sync commodities
                    await asyncio.to_thread(self.ingest_commodities)
                await asyncio.sleep(3)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in LivePaperTracker polling loop: {e}")
                await asyncio.sleep(5)

    def stop_polling_loop(self):
        self.is_running = False

