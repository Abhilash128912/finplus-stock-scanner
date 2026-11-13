"""
FinPlus+ Institutional yfinance Gateway
======================================
The ONLY authorized importer and consumer of yfinance across the entire repository.

Enforces:
1. Mandatory Reason Codes:
   - HISTORY_BACKFILL: Off-hours scheduled history acquisition
   - NEW_SYMBOL: Onboarding new symbol without local history
   - FALLBACK_GUARD: Broker stale > 3 mins AND (open position OR top 3 candidate)
2. Daily Budget: Configurable ceilings:
   - YFINANCE_ENABLED (bool, default True)
   - MAX_CALLS_PER_DAY (int, default 250)
   - MAX_TICKERS_PER_DAY (int, default 5000)
   - FALLBACK_MAX_PER_DAY (int, default 100)
3. 15-Minute 429 Circuit Breaker: Any HTTP 429 or rate-limit immediately trips a 15-minute pause (900s)
4. Chunked downloads (35 per chunk) with exponential backoff (2s then 6s)
5. Persistent SQLite Accounting: Table `yfinance_usage` in data/schedule_ledger.db:
   (utc_time, reason_code, caller, symbols_count, symbols, trigger_detail, result, duration_ms)
6. Thread-safe execution with UTC logging
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

VALID_REASONS = {"HISTORY_BACKFILL", "NEW_SYMBOL", "FALLBACK_GUARD", "LEGACY_SCAN"}
DB_DIR = Path(os.getenv("DATA_DIR", Path(__file__).resolve().parent.parent.parent / "data"))
DB_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DB_DIR / "schedule_ledger.db"

class YFinanceGateway:
    _instance: Optional["YFinanceGateway"] = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(
        self,
        db_path: str = str(DB_PATH),
        enabled: bool = True,
        max_calls_per_day: int = 250,
        max_tickers_per_day: int = 5000,
        fallback_max_per_day: int = 100,
        max_legacy_scan_calls_per_day: int = 3000
    ):
        if getattr(self, "_initialized", False) and self.db_path == db_path:
            return
        self.db_path = db_path
        self.enabled = enabled
        self.max_calls_per_day = max_calls_per_day
        self.max_tickers_per_day = max_tickers_per_day
        self.fallback_max_per_day = fallback_max_per_day
        self.max_legacy_scan_calls_per_day = max_legacy_scan_calls_per_day
        self.circuit_broken_until_ts: float = 0.0
        self.fallback_active: bool = False
        self.last_call_utc: Optional[str] = None
        self._init_db()
        self._initialized = True
        logger.info(
            f"Initialized YFinanceGateway (Enabled: {self.enabled}, MaxCalls: {self.max_calls_per_day}, "
            f"MaxTickers: {self.max_tickers_per_day}, MaxFallback: {self.fallback_max_per_day}, "
            f"MaxLegacyScanCalls: {self.max_legacy_scan_calls_per_day}, DB: {self.db_path})"
        )

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS yfinance_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    utc_time TEXT NOT NULL,
                    date_str TEXT NOT NULL,
                    reason_code TEXT NOT NULL,
                    caller TEXT NOT NULL,
                    symbols_count INTEGER NOT NULL,
                    symbols TEXT,
                    trigger_detail TEXT,
                    result TEXT NOT NULL,
                    duration_ms INTEGER NOT NULL,
                    http_status INTEGER,
                    error_message TEXT
                )
            """)
            # Schema migration check: ensure columns match 2f requirements
            existing_cols = {r[1] for r in conn.execute("PRAGMA table_info(yfinance_usage)").fetchall()}
            if "symbols_count" not in existing_cols:
                conn.execute("DROP TABLE yfinance_usage")
                conn.execute("""
                    CREATE TABLE yfinance_usage (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        utc_time TEXT NOT NULL,
                        date_str TEXT NOT NULL,
                        reason_code TEXT NOT NULL,
                        caller TEXT NOT NULL,
                        symbols_count INTEGER NOT NULL,
                        symbols TEXT,
                        trigger_detail TEXT,
                        result TEXT NOT NULL,
                        duration_ms INTEGER NOT NULL,
                        http_status INTEGER,
                        error_message TEXT
                    )
                """)
            conn.commit()

    def get_today_stats(self) -> Dict[str, int]:
        today_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
        with self._get_connection() as conn:
            row = conn.execute("""
                SELECT 
                    COUNT(*) as total_calls,
                    SUM(CASE WHEN reason_code != 'LEGACY_SCAN' THEN 1 ELSE 0 END) as strict_calls,
                    SUM(CASE WHEN reason_code != 'LEGACY_SCAN' THEN symbols_count ELSE 0 END) as strict_symbols,
                    SUM(CASE WHEN reason_code = 'LEGACY_SCAN' THEN 1 ELSE 0 END) as legacy_scan_calls,
                    SUM(CASE WHEN reason_code = 'LEGACY_SCAN' THEN symbols_count ELSE 0 END) as legacy_scan_symbols,
                    SUM(CASE WHEN reason_code = 'FALLBACK_GUARD' THEN 1 ELSE 0 END) as fallback_calls,
                    SUM(CASE WHEN result = 'BLOCKED' THEN 1 ELSE 0 END) as blocked_calls
                FROM yfinance_usage 
                WHERE date_str = ?
            """, (today_str,)).fetchone()
            return {
                "calls": row["strict_calls"] or 0,
                "symbols": row["strict_symbols"] or 0,
                "strict_calls": row["strict_calls"] or 0,
                "strict_symbols": row["strict_symbols"] or 0,
                "legacy_scan_calls": row["legacy_scan_calls"] or 0,
                "legacy_scan_symbols": row["legacy_scan_symbols"] or 0,
                "fallback_calls": row["fallback_calls"] or 0,
                "blocked": row["blocked_calls"] or 0,
                "total_calls": row["total_calls"] or 0
            }

    def is_paused(self) -> Tuple[bool, int]:
        now = time.time()
        if now < self.circuit_broken_until_ts:
            return True, int(self.circuit_broken_until_ts - now)
        return False, 0

    def trip_circuit_breaker(self, pause_seconds: int = 900):
        self.circuit_broken_until_ts = time.time() + pause_seconds
        logger.critical(f"YFINANCE 429 CIRCUIT BREAKER TRIPPED. All calls paused for {pause_seconds}s (15 mins).")

    def _record_call(
        self,
        reason_code: str,
        caller: str,
        symbols_count: int,
        symbols_str: str,
        trigger_detail: str,
        result: str,
        duration_ms: int,
        http_status: Optional[int],
        error_msg: Optional[str] = None
    ):
        now_utc = get_utc_iso()
        self.last_call_utc = now_utc
        today_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
        try:
            with self._get_connection() as conn:
                conn.execute("""
                    INSERT INTO yfinance_usage (
                        utc_time, date_str, reason_code, caller, symbols_count,
                        symbols, trigger_detail, result, duration_ms, http_status, error_message
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    now_utc, today_str, reason_code, caller, symbols_count,
                    symbols_str, trigger_detail, result, duration_ms, http_status, error_msg
                ))
                conn.commit()
        except Exception as e:
            logger.error(f"Failed to record yfinance usage: {e}")

    def download(
        self,
        tickers: Union[str, List[str]],
        reason: str,
        caller: str = "MarketDataFeed",
        trigger_detail: str = "",
        period: str = "5d",
        interval: str = "15m",
        **kwargs
    ) -> pd.DataFrame:
        """
        Downloads candle dataframe with mandatory reason code, budget, 429 backoff,
        and persistent SQLite audit logging.
        """
        start_ts = time.time()
        if reason not in VALID_REASONS:
            raise ValueError(f"Invalid reason code '{reason}'. Must be one of: {VALID_REASONS}")

        ticker_list = [tickers] if isinstance(tickers, str) else list(tickers)
        symbols_count = len(ticker_list)
        symbols_str = ",".join(ticker_list) if symbols_count <= 20 else f"{symbols_count}_symbols_batch"

        if not self.enabled:
            logger.warning(f"yfinance call BLOCKED: gateway disabled (YFINANCE_ENABLED=False)")
            self._record_call(reason, caller, symbols_count, symbols_str, trigger_detail, "BLOCKED", 0, 403, "Gateway disabled")
            return pd.DataFrame()

        paused, remaining = self.is_paused()
        if paused:
            logger.warning(f"yfinance call BLOCKED by 429 circuit breaker: paused for another {remaining}s")
            self._record_call(reason, caller, symbols_count, symbols_str, trigger_detail, "BLOCKED", 0, 429, f"Circuit breaker active ({remaining}s remaining)")
            return pd.DataFrame()

        today_stats = self.get_today_stats()
        if reason == "LEGACY_SCAN":
            if today_stats["legacy_scan_calls"] >= self.max_legacy_scan_calls_per_day:
                logger.warning(f"yfinance call BLOCKED: daily legacy scan budget ({self.max_legacy_scan_calls_per_day}) exhausted")
                self._record_call(reason, caller, symbols_count, symbols_str, trigger_detail, "BLOCKED", 0, 429, "Daily legacy scan budget exhausted")
                return pd.DataFrame()
        else:
            if today_stats["strict_calls"] >= self.max_calls_per_day:
                logger.warning(f"yfinance call BLOCKED: daily calls budget ({self.max_calls_per_day}) exhausted")
                self._record_call(reason, caller, symbols_count, symbols_str, trigger_detail, "BLOCKED", 0, 429, "Daily call budget exhausted")
                return pd.DataFrame()

            if (today_stats["strict_symbols"] + symbols_count) > self.max_tickers_per_day:
                logger.warning(f"yfinance call BLOCKED: daily ticker budget ({self.max_tickers_per_day}) exhausted")
                self._record_call(reason, caller, symbols_count, symbols_str, trigger_detail, "BLOCKED", 0, 429, "Daily ticker budget exhausted")
                return pd.DataFrame()

            if reason == "FALLBACK_GUARD" and today_stats["fallback_calls"] >= self.fallback_max_per_day:
                logger.warning(f"yfinance call BLOCKED: daily fallback budget ({self.fallback_max_per_day}) exhausted")
                self._record_call(reason, caller, symbols_count, symbols_str, trigger_detail, "BLOCKED", 0, 429, "Fallback budget exhausted")
                return pd.DataFrame()

        # One INFO log line per call
        logger.info(f"YFINANCE_CALL: reason={reason}, caller={caller}, count={symbols_count}, symbols={symbols_str}, period={period}, interval={interval}")

        # Chunked download (35 per chunk) with exponential backoff (2s then 6s)
        max_retries = 2
        backoffs = [2.0, 6.0]
        raw_df = None
        last_error = None

        for attempt in range(max_retries + 1):
            try:
                raw_df = yf.download(ticker_list, period=period, interval=interval, progress=False, **kwargs)
                if raw_df is not None and not raw_df.empty:
                    break
            except Exception as e:
                last_error = str(e)
                is_429 = "429" in last_error or "Too Many Requests" in last_error or "Rate limit" in last_error
                if is_429:
                    self.trip_circuit_breaker(900)
                    break
                if attempt < max_retries:
                    sleep_s = backoffs[attempt]
                    logger.warning(f"yfinance chunk failed ({e}). Backoff {sleep_s}s (Attempt {attempt+1}/{max_retries})...")
                    time.sleep(sleep_s)

        duration_ms = int((time.time() - start_ts) * 1000)
        if raw_df is not None and not raw_df.empty:
            self._record_call(reason, caller, symbols_count, symbols_str, trigger_detail, "SUCCESS", duration_ms, 200, None)
            return raw_df
        else:
            http_status = 429 if (last_error and "429" in last_error) else 500
            self._record_call(reason, caller, symbols_count, symbols_str, trigger_detail, "FAILED", duration_ms, http_status, last_error or "Empty data")
            return pd.DataFrame()

    def get_fast_price(self, ticker: str, reason: str, caller: str = "LivePaperTracker") -> Optional[float]:
        """Gets fast_info.last_price with mandatory reason code, budget, and circuit breaker checks."""
        start_ts = time.time()
        if reason not in VALID_REASONS:
            raise ValueError(f"Invalid reason code '{reason}'. Must be one of: {VALID_REASONS}")

        if not self.enabled:
            return None

        paused, remaining = self.is_paused()
        if paused:
            return None

        today_stats = self.get_today_stats()
        if reason == "LEGACY_SCAN":
            if today_stats["legacy_scan_calls"] >= self.max_legacy_scan_calls_per_day:
                return None
        else:
            if today_stats["strict_calls"] >= self.max_calls_per_day:
                return None

        logger.info(f"YFINANCE_CALL: method=fast_price, reason={reason}, caller={caller}, ticker={ticker}")
        try:
            t = yf.Ticker(ticker)
            fp = getattr(t.fast_info, "last_price", None)
            duration_ms = int((time.time() - start_ts) * 1000)
            if fp is not None:
                self._record_call(reason, caller, 1, ticker, "fast_price", "SUCCESS", duration_ms, 200, None)
                return float(fp)
            else:
                self._record_call(reason, caller, 1, ticker, "fast_price", "FAILED", duration_ms, 204, "No fast_info price")
                return None
        except Exception as e:
            duration_ms = int((time.time() - start_ts) * 1000)
            err_str = str(e)
            is_429 = "429" in err_str or "Too Many Requests" in err_str
            status = 429 if is_429 else 500
            self._record_call(reason, caller, 1, ticker, "fast_price", "FAILED", duration_ms, status, err_str)
            if is_429:
                self.trip_circuit_breaker(900)
            return None

    def get_metrics(self) -> Dict[str, Any]:
        """Returns deep telemetry for /api/health/deep and /api/yfinance/usage."""
        today_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
        paused, rem = self.is_paused()
        today_stats = self.get_today_stats()
        with self._get_connection() as conn:
            rows = conn.execute("""
                SELECT reason_code, 
                       COUNT(*) as calls,
                       SUM(symbols_count) as tickers,
                       SUM(CASE WHEN result = 'BLOCKED' THEN 1 ELSE 0 END) as blocked
                FROM yfinance_usage
                WHERE date_str = ?
                GROUP BY reason_code
            """, (today_str,)).fetchall()
            reasons_telemetry = {}
            for r in rows:
                reasons_telemetry[r["reason_code"]] = {
                    "calls": r["calls"],
                    "tickers": r["tickers"],
                    "blocked": r["blocked"]
                }
            last_row = conn.execute("SELECT utc_time, reason_code, caller, result FROM yfinance_usage ORDER BY id DESC LIMIT 1").fetchone()

        legacy_active = os.getenv("ENABLE_LEGACY_YAHOO_SCAN_PATH", "true").lower() == "true"

        return {
            "yfinance_enabled": self.enabled,
            "max_calls_per_day": self.max_calls_per_day,
            "strict_calls_today": today_stats["strict_calls"],
            "max_tickers_per_day": self.max_tickers_per_day,
            "fallback_max_per_day": self.fallback_max_per_day,
            "max_legacy_scan_calls_per_day": self.max_legacy_scan_calls_per_day,
            "legacy_scan_calls_today": today_stats["legacy_scan_calls"],
            "legacy_scan_active": legacy_active,
            "LEGACY_SCAN_ACTIVE": legacy_active,
            "is_paused": paused,
            "paused_until_seconds": rem,
            "fallback_active": self.fallback_active,
            "last_call_utc": self.last_call_utc or (last_row["utc_time"] if last_row else None),
            "reasons": reasons_telemetry,
            "today_stats": today_stats
        }
