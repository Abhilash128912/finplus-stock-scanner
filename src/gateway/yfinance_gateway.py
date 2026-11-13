"""
FinPlus+ Institutional yfinance Gateway
======================================
The ONLY authorized importer and consumer of yfinance across the entire repository.

Enforces:
1. Mandatory Reason Codes:
   - HISTORY_BACKFILL: Off-hours scheduled history acquisition
   - NEW_SYMBOL: Onboarding new symbol without local history
   - FALLBACK_GUARD: Broker stale > 3 mins AND (open position OR top 3 candidate)
2. Daily Budget: Default 250 requests/day max
3. 15-Minute 429 Circuit Breaker: Any HTTP 429 or rate-limit immediately trips a 15-minute pause
4. Persistent SQLite Accounting: Table `yfinance_usage` in data/schedule_ledger.db
5. Thread-safe execution with UTC logging
"""

import os
import time
import sqlite3
import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any, Union, Tuple
import pandas as pd

# The ONLY permitted direct import of yfinance in the codebase
import yfinance as yf

from src.utils.time_and_logger import setup_logger, get_utc_iso

logger = setup_logger("YFinanceGateway")

VALID_REASONS = {"HISTORY_BACKFILL", "NEW_SYMBOL", "FALLBACK_GUARD"}
DB_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "schedule_ledger.db"

class YFinanceGateway:
    _instance: Optional["YFinanceGateway"] = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self, db_path: str = str(DB_PATH), daily_budget: int = 250):
        if getattr(self, "_initialized", False):
            return
        self.db_path = db_path
        self.daily_budget = daily_budget
        self.circuit_broken_until_ts: float = 0.0
        self._init_db()
        self._initialized = True
        logger.info(f"Initialized YFinanceGateway (Daily Budget: {self.daily_budget}, DB: {self.db_path})")

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS yfinance_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    call_utc TEXT NOT NULL,
                    date_str TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    method TEXT NOT NULL,
                    success INTEGER NOT NULL,
                    http_status INTEGER,
                    error_message TEXT
                )
            """)
            conn.commit()

    def get_today_call_count(self) -> int:
        today_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
        with self._get_connection() as conn:
            row = conn.execute("SELECT COUNT(*) FROM yfinance_usage WHERE date_str = ? AND success = 1", (today_str,)).fetchone()
            return row[0] if row else 0

    def is_paused(self) -> Tuple[bool, int]:
        now = time.time()
        if now < self.circuit_broken_until_ts:
            return True, int(self.circuit_broken_until_ts - now)
        return False, 0

    def trip_circuit_breaker(self, pause_seconds: int = 900):
        self.circuit_broken_until_ts = time.time() + pause_seconds
        logger.critical(f"YFINANCE 429 CIRCUIT BREAKER TRIPPED. All calls paused for {pause_seconds}s (15 mins).")

    def _record_call(self, reason: str, ticker: str, method: str, success: bool, http_status: Optional[int], error_msg: Optional[str] = None):
        now_utc = get_utc_iso()
        today_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
        try:
            with self._get_connection() as conn:
                conn.execute("""
                    INSERT INTO yfinance_usage (call_utc, date_str, reason, ticker, method, success, http_status, error_message)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (now_utc, today_str, reason, str(ticker), method, 1 if success else 0, http_status, error_msg))
                conn.commit()
        except Exception as e:
            logger.error(f"Failed to record yfinance usage: {e}")

    def download(self, tickers: Union[str, List[str]], reason: str, period: str = "5d", interval: str = "15m", **kwargs) -> pd.DataFrame:
        """Downloads candle dataframe with mandatory reason code, budget, and circuit breaker checks."""
        if reason not in VALID_REASONS:
            raise ValueError(f"Invalid reason code '{reason}'. Must be one of: {VALID_REASONS}")

        paused, remaining = self.is_paused()
        if paused:
            logger.warning(f"yfinance call REJECTED by circuit breaker: paused for another {remaining}s")
            return pd.DataFrame()

        today_count = self.get_today_call_count()
        if today_count >= self.daily_budget:
            logger.warning(f"yfinance call REJECTED: daily budget of {self.daily_budget} exhausted (used: {today_count})")
            return pd.DataFrame()

        ticker_str = ",".join(tickers) if isinstance(tickers, list) else tickers
        logger.info(f"YFINANCE_CALL: method=download, reason={reason}, tickers={ticker_str}, period={period}, interval={interval}")

        try:
            df = yf.download(tickers, period=period, interval=interval, progress=False, **kwargs)
            self._record_call(reason, ticker_str, "download", True, 200, None)
            return df
        except Exception as e:
            err_str = str(e)
            is_429 = "429" in err_str or "Too Many Requests" in err_str or "Rate limit" in err_str
            status = 429 if is_429 else 500
            self._record_call(reason, ticker_str, "download", False, status, err_str)
            if is_429:
                self.trip_circuit_breaker(900)
            logger.error(f"yfinance download failed ({reason}, {ticker_str}): {e}")
            return pd.DataFrame()

    def get_fast_price(self, ticker: str, reason: str) -> Optional[float]:
        """Gets fast_info.last_price with mandatory reason code, budget, and circuit breaker checks."""
        if reason not in VALID_REASONS:
            raise ValueError(f"Invalid reason code '{reason}'. Must be one of: {VALID_REASONS}")

        paused, remaining = self.is_paused()
        if paused:
            logger.warning(f"yfinance call REJECTED by circuit breaker: paused for another {remaining}s")
            return None

        today_count = self.get_today_call_count()
        if today_count >= self.daily_budget:
            logger.warning(f"yfinance call REJECTED: daily budget exhausted ({today_count}/{self.daily_budget})")
            return None

        logger.info(f"YFINANCE_CALL: method=fast_price, reason={reason}, ticker={ticker}")
        try:
            t = yf.Ticker(ticker)
            fp = getattr(t.fast_info, "last_price", None)
            if fp is not None:
                self._record_call(reason, ticker, "fast_price", True, 200, None)
                return float(fp)
            else:
                self._record_call(reason, ticker, "fast_price", False, 204, "No fast_info price")
                return None
        except Exception as e:
            err_str = str(e)
            is_429 = "429" in err_str or "Too Many Requests" in err_str
            status = 429 if is_429 else 500
            self._record_call(reason, ticker, "fast_price", False, status, err_str)
            if is_429:
                self.trip_circuit_breaker(900)
            return None

    def get_metrics(self) -> Dict[str, Any]:
        """Returns diagnostic usage telemetry."""
        today_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
        paused, rem = self.is_paused()
        with self._get_connection() as conn:
            total_today = conn.execute("SELECT COUNT(*) FROM yfinance_usage WHERE date_str = ?", (today_str,)).fetchone()[0]
            success_today = conn.execute("SELECT COUNT(*) FROM yfinance_usage WHERE date_str = ? AND success = 1", (today_str,)).fetchone()[0]
            failed_today = total_today - success_today
            reasons = dict(conn.execute("SELECT reason, COUNT(*) FROM yfinance_usage WHERE date_str = ? GROUP BY reason", (today_str,)).fetchall())

        return {
            "daily_budget": self.daily_budget,
            "calls_today": total_today,
            "success_today": success_today,
            "failed_today": failed_today,
            "is_paused": paused,
            "pause_remaining_seconds": rem,
            "calls_by_reason": reasons
        }
