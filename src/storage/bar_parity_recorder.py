"""
FinPlus+ Bar Parity Recorder (Phase 2C)
=======================================
Compares completed 15m and 5m candle bars built by CandleStore (from INDmoney quotes)
against corresponding Yahoo Finance bars during market hours while legacy scan is active.
- Records: open/high/low/close/volume percentage deviations and missing bar sources in SQLite table `bar_parity`.
- Tolerances:
    * Close within 0.1%
    * High/Low within 0.3%
    * Volume within 5.0%
- READY Rule: Reports "READY TO SWITCH OFF YAHOO" only when at least 98% of scanned symbols
  have complete INDmoney bars and at least 99% of bars are within tolerance for 2 consecutive sessions.
"""

import os
import sqlite3
import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

from src.utils.time_and_logger import setup_logger, get_utc_iso

logger = setup_logger("BarParityRecorder")

DB_DIR = Path(os.getenv("DATA_DIR", Path(__file__).resolve().parent.parent.parent / "data"))
DB_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DB_DIR / "candles.db"


class BarParityRecorder:
    def __init__(self, db_path: str = str(DB_PATH)):
        self.db_path = db_path
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS bar_parity (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date_str TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    bar_time_utc TEXT NOT NULL,
                    bar_time_ist TEXT NOT NULL,
                    epoch INTEGER NOT NULL,
                    ind_open REAL,
                    ind_high REAL,
                    ind_low REAL,
                    ind_close REAL,
                    ind_volume INTEGER,
                    yf_open REAL,
                    yf_high REAL,
                    yf_low REAL,
                    yf_close REAL,
                    yf_volume INTEGER,
                    open_dev_pct REAL,
                    high_dev_pct REAL,
                    low_dev_pct REAL,
                    close_dev_pct REAL,
                    volume_dev_pct REAL,
                    missing_source TEXT,
                    is_within_tolerance INTEGER NOT NULL DEFAULT 1,
                    created_at_utc TEXT NOT NULL,
                    UNIQUE(symbol, timeframe, epoch)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS bar_parity_sessions (
                    date_str TEXT PRIMARY KEY,
                    complete_symbols_pct REAL NOT NULL,
                    bars_within_tolerance_pct REAL NOT NULL,
                    is_session_ready INTEGER NOT NULL DEFAULT 0,
                    evaluated_at_utc TEXT NOT NULL
                )
            """)
            conn.commit()

    def record_bar_comparison(
        self,
        symbol: str,
        timeframe: str,
        epoch: int,
        ind_bar: Optional[Dict[str, Any]],
        yf_bar: Optional[Dict[str, Any]],
        date_str: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Calculates bar deviations between INDmoney CandleStore and Yahoo Finance.
        Persists into bar_parity SQLite table.
        """
        clean_sym = symbol.split(" ")[0].upper()
        utc_dt = datetime.datetime.fromtimestamp(epoch, tz=datetime.timezone.utc)
        ist_dt = utc_dt + datetime.timedelta(hours=5, minutes=30)
        now_date_str = date_str or ist_dt.strftime("%Y-%m-%d")

        missing_source = None
        open_dev = high_dev = low_dev = close_dev = vol_dev = 0.0
        is_tol = 1

        if ind_bar is None and yf_bar is not None:
            missing_source = "INDMONEY_MISSING"
            is_tol = 0
            ind_o = ind_h = ind_l = ind_c = None
            ind_v = None
            yf_o, yf_h, yf_l, yf_c, yf_v = (
                float(yf_bar.get("open", 0.0)), float(yf_bar.get("high", 0.0)),
                float(yf_bar.get("low", 0.0)), float(yf_bar.get("close", 0.0)),
                int(yf_bar.get("volume", 0))
            )
        elif yf_bar is None and ind_bar is not None:
            missing_source = "YAHOO_MISSING"
            is_tol = 0
            yf_o = yf_h = yf_l = yf_c = None
            yf_v = None
            ind_o, ind_h, ind_l, ind_c, ind_v = (
                float(ind_bar.get("open", 0.0)), float(ind_bar.get("high", 0.0)),
                float(ind_bar.get("low", 0.0)), float(ind_bar.get("close", 0.0)),
                int(ind_bar.get("volume", 0))
            )
        elif ind_bar is not None and yf_bar is not None:
            ind_o, ind_h, ind_l, ind_c, ind_v = (
                float(ind_bar.get("open", 0.0)), float(ind_bar.get("high", 0.0)),
                float(ind_bar.get("low", 0.0)), float(ind_bar.get("close", 0.0)),
                int(ind_bar.get("volume", 0))
            )
            yf_o, yf_h, yf_l, yf_c, yf_v = (
                float(yf_bar.get("open", 0.0)), float(yf_bar.get("high", 0.0)),
                float(yf_bar.get("low", 0.0)), float(yf_bar.get("close", 0.0)),
                int(yf_bar.get("volume", 0))
            )

            # Compute percentage deviations relative to reference bar
            ref_c = yf_c if yf_c > 0 else ind_c
            close_dev = round(abs(ind_c - yf_c) / ref_c * 100.0, 3) if ref_c > 0 else 0.0

            ref_h = yf_h if yf_h > 0 else ind_h
            high_dev = round(abs(ind_h - yf_h) / ref_h * 100.0, 3) if ref_h > 0 else 0.0

            ref_l = yf_l if yf_l > 0 else ind_l
            low_dev = round(abs(ind_l - yf_l) / ref_l * 100.0, 3) if ref_l > 0 else 0.0

            ref_o = yf_o if yf_o > 0 else ind_o
            open_dev = round(abs(ind_o - yf_o) / ref_o * 100.0, 3) if ref_o > 0 else 0.0

            ref_v = yf_v if yf_v > 0 else ind_v
            vol_dev = round(abs(ind_v - yf_v) / ref_v * 100.0, 2) if ref_v > 0 else 0.0

            # Tolerances: Close <= 0.1%, High/Low <= 0.3%, Volume <= 5.0%
            is_tol = 1 if (close_dev <= 0.1 and high_dev <= 0.3 and low_dev <= 0.3 and vol_dev <= 5.0) else 0
        else:
            return {}

        now_utc = get_utc_iso()
        with self._get_connection() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO bar_parity (
                    date_str, symbol, timeframe, bar_time_utc, bar_time_ist, epoch,
                    ind_open, ind_high, ind_low, ind_close, ind_volume,
                    yf_open, yf_high, yf_low, yf_close, yf_volume,
                    open_dev_pct, high_dev_pct, low_dev_pct, close_dev_pct, volume_dev_pct,
                    missing_source, is_within_tolerance, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                now_date_str, clean_sym, timeframe,
                utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                ist_dt.strftime("%Y-%m-%d %H:%M:%S"),
                epoch, ind_o, ind_h, ind_l, ind_c, ind_v,
                yf_o, yf_h, yf_l, yf_c, yf_v,
                open_dev, high_dev, low_dev, close_dev, vol_dev,
                missing_source, is_tol, now_utc
            ))
            conn.commit()

        return {
            "symbol": clean_sym,
            "epoch": epoch,
            "close_dev_pct": close_dev,
            "high_dev_pct": high_dev,
            "low_dev_pct": low_dev,
            "volume_dev_pct": vol_dev,
            "is_within_tolerance": is_tol,
            "missing_source": missing_source
        }

    def get_daily_summary(self, date_str: Optional[str] = None) -> Dict[str, Any]:
        """Calculates daily bar parity audit metrics and READY rule status."""
        if not date_str:
            utc_now = datetime.datetime.now(datetime.timezone.utc)
            date_str = (utc_now + datetime.timedelta(hours=5, minutes=30)).strftime("%Y-%m-%d")

        with self._get_connection() as conn:
            # Total comparisons
            total_rows = conn.execute("SELECT COUNT(*) FROM bar_parity WHERE date_str = ?", (date_str,)).fetchone()[0]
            if total_rows == 0:
                return {
                    "date": date_str,
                    "total_compared_bars": 0,
                    "symbols_with_complete_ind_bars_pct": 0.0,
                    "close_within_01_pct": 0.0,
                    "high_low_within_03_pct": 0.0,
                    "volume_within_05_pct": 0.0,
                    "missing_ind_bars": 0,
                    "missing_yf_bars": 0,
                    "ready_to_switch_off_yahoo": False,
                    "ready_status": "NOT READY (No bar parity data recorded for today)",
                    "worst_10_symbols": []
                }

            # Metrics
            missing_ind = conn.execute("SELECT COUNT(*) FROM bar_parity WHERE date_str = ? AND missing_source = 'INDMONEY_MISSING'", (date_str,)).fetchone()[0]
            missing_yf = conn.execute("SELECT COUNT(*) FROM bar_parity WHERE date_str = ? AND missing_source = 'YAHOO_MISSING'", (date_str,)).fetchone()[0]

            close_ok = conn.execute("SELECT COUNT(*) FROM bar_parity WHERE date_str = ? AND missing_source IS NULL AND close_dev_pct <= 0.1", (date_str,)).fetchone()[0]
            hl_ok = conn.execute("SELECT COUNT(*) FROM bar_parity WHERE date_str = ? AND missing_source IS NULL AND high_dev_pct <= 0.3 AND low_dev_pct <= 0.3", (date_str,)).fetchone()[0]
            vol_ok = conn.execute("SELECT COUNT(*) FROM bar_parity WHERE date_str = ? AND missing_source IS NULL AND volume_dev_pct <= 5.0", (date_str,)).fetchone()[0]
            all_tol_ok = conn.execute("SELECT COUNT(*) FROM bar_parity WHERE date_str = ? AND is_within_tolerance = 1", (date_str,)).fetchone()[0]

            # Unique symbols
            total_symbols = conn.execute("SELECT COUNT(DISTINCT symbol) FROM bar_parity WHERE date_str = ?", (date_str,)).fetchone()[0]
            symbols_missing_ind = conn.execute("SELECT COUNT(DISTINCT symbol) FROM bar_parity WHERE date_str = ? AND missing_source = 'INDMONEY_MISSING'", (date_str,)).fetchone()[0]
            complete_symbols = max(0, total_symbols - symbols_missing_ind)
            complete_pct = round((complete_symbols / total_symbols * 100.0) if total_symbols > 0 else 0.0, 2)

            close_pct = round(close_ok / total_rows * 100.0, 2)
            hl_pct = round(hl_ok / total_rows * 100.0, 2)
            vol_pct = round(vol_ok / total_rows * 100.0, 2)
            tol_pct = round(all_tol_ok / total_rows * 100.0, 2)

            # Worst 10 symbols
            worst_rows = conn.execute("""
                SELECT symbol, 
                       MAX(close_dev_pct) as max_close_dev, 
                       MAX(high_dev_pct) as max_high_dev, 
                       MAX(low_dev_pct) as max_low_dev,
                       MAX(volume_dev_pct) as max_vol_dev,
                       COUNT(*) as bar_count
                FROM bar_parity 
                WHERE date_str = ? AND missing_source IS NULL
                GROUP BY symbol 
                ORDER BY (max_close_dev + max_high_dev + max_low_dev) DESC 
                LIMIT 10
            """, (date_str,)).fetchall()
            worst_symbols = [
                {
                    "symbol": r["symbol"],
                    "max_close_dev_pct": r["max_close_dev"],
                    "max_high_dev_pct": r["max_high_dev"],
                    "max_low_dev_pct": r["max_low_dev"],
                    "max_volume_dev_pct": r["max_vol_dev"],
                    "bars_compared": r["bar_count"]
                }
                for r in worst_rows
            ]

            # READY rule: >= 98% symbols have complete bars AND >= 99% bars within tolerance
            is_session_ready = 1 if (complete_pct >= 98.0 and tol_pct >= 99.0) else 0

            # Check consecutive ready sessions in table bar_parity_sessions
            conn.execute("""
                INSERT OR REPLACE INTO bar_parity_sessions
                (date_str, complete_symbols_pct, bars_within_tolerance_pct, is_session_ready, evaluated_at_utc)
                VALUES (?, ?, ?, ?, ?)
            """, (date_str, complete_pct, tol_pct, is_session_ready, get_utc_iso()))
            conn.commit()

            recent_ready = conn.execute("""
                SELECT is_session_ready FROM bar_parity_sessions ORDER BY date_str DESC LIMIT 2
            """).fetchall()
            consecutive_ready = sum(r[0] for r in recent_ready) if len(recent_ready) >= 2 else is_session_ready

            ready_flag = (consecutive_ready >= 2)
            ready_msg = "READY TO SWITCH OFF YAHOO (2 consecutive sessions verified)" if ready_flag else (
                f"NOT READY (Session {consecutive_ready}/2: {complete_pct}% symbols complete, {tol_pct}% within tolerance)"
            )

        return {
            "date": date_str,
            "total_compared_bars": total_rows,
            "total_symbols_evaluated": total_symbols,
            "symbols_with_complete_ind_bars_pct": complete_pct,
            "bars_with_close_within_01_pct": close_pct,
            "bars_with_high_low_within_03_pct": hl_pct,
            "bars_with_volume_within_05_pct": vol_pct,
            "bars_within_tolerance_pct": tol_pct,
            "missing_ind_bars": missing_ind,
            "missing_yf_bars": missing_yf,
            "ready_to_switch_off_yahoo": ready_flag,
            "ready_status": ready_msg,
            "worst_10_symbols": worst_symbols
        }

