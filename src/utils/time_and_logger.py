"""
FinPlus+ Stock Scanner
Timezone & Structured UTC Logging Engine
Compliance:
- Internal logs, audit records, and JSON payloads MUST be recorded in ISO-8601 UTC ('Z').
- Market hours checks and intraday logic MUST be evaluated in Indian Standard Time (IST, UTC+05:30).
"""

import os
import sys
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

IST_TZ = ZoneInfo("Asia/Kolkata")
UTC_TZ = timezone.utc

def get_utc_now() -> datetime:
    """Returns current timezone-aware UTC datetime, with optional FAKED_SYSTEM_IST override strictly under pytest."""
    if "pytest" in sys.modules or os.getenv("PYTEST_CURRENT_TEST"):
        faked = os.getenv("FAKED_SYSTEM_IST")
        if faked:
            try:
                return datetime.fromisoformat(faked).astimezone(UTC_TZ)
            except Exception:
                pass
    return datetime.now(UTC_TZ)

def get_utc_iso() -> str:
    """Returns ISO-8601 UTC formatted string (e.g. 2026-11-08T07:45:00.123456Z)."""
    return get_utc_now().strftime("%Y-%m-%dT%H:%M:%S.%fZ")

def get_ist_now() -> datetime:
    """Returns current timezone-aware IST datetime, with optional FAKED_SYSTEM_IST override strictly under pytest."""
    if "pytest" in sys.modules or os.getenv("PYTEST_CURRENT_TEST"):
        faked = os.getenv("FAKED_SYSTEM_IST")
        if faked:
            try:
                return datetime.fromisoformat(faked).astimezone(IST_TZ)
            except Exception:
                pass
    return datetime.now(IST_TZ)

def get_ist_time_str() -> str:
    """Returns human-readable IST time (e.g. 01:15 PM IST)."""
    return get_ist_now().strftime("%I:%M %p IST")

def is_market_open_ist() -> bool:
    """Checks whether current IST time is within active market hours and not an official NSE holiday."""
    from src.utils.indian_market_calendar import is_equity_market_open_ist
    is_open, _ = is_equity_market_open_ist()
    return is_open

def is_pulse_window_ist() -> bool:
    """Checks whether current IST time is within 30-minute pulse window (09:15 to 15:00 IST) on an active market day."""
    from src.utils.indian_market_calendar import is_nse_holiday, get_current_ist_datetime
    now_ist = get_current_ist_datetime()
    if now_ist.weekday() >= 5:
        return False
    is_hol, _ = is_nse_holiday(now_ist.date())
    if is_hol:
        return False
    curr_time = now_ist.time()
    return time(9, 15) <= curr_time <= time(15, 0)

def is_ignition_window_ist() -> bool:
    """Checks whether current IST time is in active ignition scan window (09:30 to 14:45 IST) on an active market day."""
    from src.utils.indian_market_calendar import is_nse_holiday, get_current_ist_datetime
    now_ist = get_current_ist_datetime()
    if now_ist.weekday() >= 5:
        return False
    is_hol, _ = is_nse_holiday(now_ist.date())
    if is_hol:
        return False
    curr_time = now_ist.time()
    return time(9, 30) <= curr_time <= time(14, 45)

def is_intraday_squareoff_time_ist() -> bool:
    """Checks whether current IST time is at or after intraday square-off (15:15 IST)."""
    now_ist = get_ist_now()
    return now_ist.time() >= time(15, 15)

class UTCFormatter(logging.Formatter):
    """Custom logging formatter converting log timestamps strictly to ISO-8601 UTC."""
    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, tz=UTC_TZ)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

class SafeRotatingFileHandler(RotatingFileHandler):
    """Windows-safe rotating file handler that catches and ignores file lock rollover conflicts."""
    def doRollover(self):
        try:
            super().doRollover()
        except (PermissionError, OSError):
            pass

def setup_logger(name: str = "FinPlus_StockScanner") -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    
    if not logger.handlers:
        formatter = UTCFormatter("[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s")
        
        # Console Handler
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(formatter)
        logger.addHandler(ch)
        
        # Safe Rotating File Handler in logs/
        log_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "logs"))
        os.makedirs(log_dir, exist_ok=True)
        log_file = os.path.join(log_dir, "stock_scanner_utc.log")
        
        fh = SafeRotatingFileHandler(log_file, maxBytes=10*1024*1024, backupCount=5, encoding="utf-8", delay=True)
        fh.setFormatter(formatter)
        logger.addHandler(fh)
        
    return logger
