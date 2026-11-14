"""
FinPlus+ Official Indian Financial Markets Calendar
Manages official NSE/BSE and MCX trading holidays, session timings, and live market heartbeat checks.
Prevents false breakout generation, stale candle processing, and phantom trades on market holidays.
"""

import datetime
import json
import os
import sys
from pathlib import Path
from typing import Dict, Any, Tuple, Optional, Union
import pytz

IST = pytz.timezone("Asia/Kolkata")

# Path to editable holidays JSON
CONFIG_DIR = Path(__file__).resolve().parent.parent.parent / "config"
HOLIDAYS_JSON_PATH = CONFIG_DIR / "nse_holidays.json"

# Load holidays and special sessions from config/nse_holidays.json if present
def load_nse_holidays_from_file() -> Tuple[Dict[str, str], Dict[str, Any]]:
    """Loads editable holiday list and special sessions from config/nse_holidays.json."""
    if HOLIDAYS_JSON_PATH.exists():
        try:
            with open(HOLIDAYS_JSON_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("holidays", {}), data.get("special_sessions", {})
        except Exception:
            pass
    return {}, {}

_FILE_HOLIDAYS, _FILE_SPECIAL_SESSIONS = load_nse_holidays_from_file()

# Official NSE / BSE Trading Holidays (Equity, F&O, SLB)
# Formatted as YYYY-MM-DD: Holiday Name
NSE_TRADING_HOLIDAYS: Dict[str, str] = {
    # 2025 Holidays
    "2025-01-26": "Republic Day",
    "2025-02-26": "Mahashivratri",
    "2025-03-14": "Holi",
    "2025-03-31": "Id-Ul-Fitr",
    "2025-04-10": "Shri Mahavir Jayanti",
    "2025-04-14": "Dr. Ambedkar Jayanti",
    "2025-04-18": "Good Friday",
    "2025-05-01": "Maharashtra Day",
    "2025-06-07": "Bakri Id",
    "2025-08-15": "Independence Day",
    "2025-08-27": "Ganesh Chaturthi",
    "2025-10-02": "Mahatma Gandhi Jayanti",
    "2025-10-21": "Dussehra",
    "2025-11-01": "Diwali Laxmi Pujan (Muhurat Trading only)",
    "2025-11-03": "Diwali Balipratipada",
    "2025-11-05": "Guru Nanak Jayanti",
    "2025-12-25": "Christmas",

    # 2026 Official NSE Calendar
    "2026-01-26": "Republic Day",
    "2026-02-15": "Mahashivratri",
    "2026-03-03": "Holi",
    "2026-03-21": "Id-Ul-Fitr (Ramadan Eid)",
    "2026-03-26": "Shri Ram Navami",
    "2026-03-31": "Shri Mahavir Jayanti",
    "2026-04-03": "Good Friday",
    "2026-04-14": "Dr. Baba Saheb Ambedkar Jayanti",
    "2026-05-01": "Maharashtra Day",
    "2026-05-28": "Bakri Id",
    "2026-06-26": "Muharram",
    "2026-08-15": "Independence Day",
    "2026-09-14": "Ganesh Chaturthi",
    "2026-10-02": "Mahatma Gandhi Jayanti",
    "2026-10-20": "Dussehra",
    "2026-11-08": "Diwali Laxmi Pujan (Muhurat Trading only)",
    "2026-11-10": "Diwali-Balipratipada",
    "2026-11-24": "Prakash Gurpurb Sri Guru Nanak Dev",
    "2026-12-25": "Christmas",

    # 2027 Anticipated / Common Dates
    "2027-01-26": "Republic Day",
    "2027-03-23": "Holi",
    "2027-03-26": "Good Friday",
    "2027-04-14": "Dr. Ambedkar Jayanti",
    "2027-05-01": "Maharashtra Day",
    "2027-08-15": "Independence Day",
    "2027-10-02": "Mahatma Gandhi Jayanti",
    "2027-10-11": "Dussehra",
    "2027-10-30": "Diwali Laxmi Pujan",
    "2027-11-01": "Diwali Balipratipada",
    "2027-12-25": "Christmas"
}
NSE_TRADING_HOLIDAYS.update(_FILE_HOLIDAYS)
SPECIAL_TRADING_SESSIONS: Dict[str, Any] = _FILE_SPECIAL_SESSIONS

# MCX Holidays where Morning Session (09:00 - 17:00 IST) is closed
# Evening session opens at 17:00 IST
MCX_MORNING_CLOSED_HOLIDAYS = {
    "2026-03-03": "Holi",
    "2026-03-26": "Shri Ram Navami",
    "2026-03-31": "Shri Mahavir Jayanti",
    "2026-04-14": "Dr. Ambedkar Jayanti",
    "2026-05-01": "Maharashtra Day",
    "2026-05-28": "Bakri Id",
    "2026-06-26": "Muharram",
    "2026-09-14": "Ganesh Chaturthi",
    "2026-10-20": "Dussehra",
    "2026-11-10": "Diwali-Balipratipada",
    "2026-11-24": "Guru Nanak Jayanti"
}

# MCX Holidays where BOTH Morning & Evening Sessions are closed
MCX_FULL_DAY_HOLIDAYS = {
    "2026-01-26": "Republic Day",
    "2026-04-03": "Good Friday",
    "2026-08-15": "Independence Day",
    "2026-10-02": "Mahatma Gandhi Jayanti",
    "2026-12-25": "Christmas"
}


def get_current_ist_datetime() -> datetime.datetime:
    """Returns timezone-aware current datetime in IST, with optional FAKED_SYSTEM_IST override strictly under pytest."""
    if "pytest" in sys.modules or os.getenv("PYTEST_CURRENT_TEST"):
        faked = os.getenv("FAKED_SYSTEM_IST")
        if faked:
            try:
                return datetime.datetime.fromisoformat(faked).astimezone(IST)
            except Exception:
                pass
    return datetime.datetime.now(tz=IST)


def is_nse_holiday(check_date: Union[datetime.date, str, None] = None) -> Tuple[bool, str]:
    """
    Checks if given date is an official NSE trading holiday.
    Accepts datetime.date, datetime.datetime, YYYY-MM-DD str, or None (defaults to today in IST).
    Returns (True, Holiday Name) or (False, '').
    """
    if check_date is None:
        check_date = get_current_ist_datetime().date()
    elif isinstance(check_date, str):
        try:
            check_date = datetime.datetime.strptime(check_date, "%Y-%m-%d").date()
        except Exception:
            return False, ""
    elif isinstance(check_date, datetime.datetime):
        check_date = check_date.date()

    date_str = check_date.strftime("%Y-%m-%d")
    if date_str in NSE_TRADING_HOLIDAYS:
        return True, NSE_TRADING_HOLIDAYS[date_str]
    return False, ""


def is_equity_market_open_ist() -> Tuple[bool, str]:
    """
    Checks if NSE/BSE Equity & F&O markets are currently open.
    Enforces:
    1. Weekday check (Mon-Fri)
    2. Official NSE holiday calendar
    3. Session hours: 09:15 to 15:30 IST
    """
    now_ist = get_current_ist_datetime()
    curr_date = now_ist.date()
    curr_time = now_ist.time()

    # Weekend check
    if now_ist.weekday() >= 5:
        return False, "Weekend (Saturday/Sunday) - Exchange Closed"

    # Official NSE Holiday check
    is_hol, hol_name = is_nse_holiday(curr_date)
    if is_hol:
        return False, f"NSE Trading Holiday ({hol_name}) - Exchange Closed"

    # Market timing check
    if not (datetime.time(9, 15) <= curr_time <= datetime.time(15, 30)):
        return False, f"Off-Hours (Current IST: {curr_time.strftime('%H:%M:%S')}, Market: 09:15 - 15:30 IST)"

    return True, "NSE Market Open"


def is_mcx_market_open_ist() -> Tuple[bool, str]:
    """
    Checks if MCX Commodity market is currently open.
    Handles:
    1. Full-day MCX holidays
    2. Morning-closed holidays (opens at 17:00 IST)
    3. Regular sessions (09:00 - 23:30 IST)
    """
    now_ist = get_current_ist_datetime()
    curr_date = now_ist.date()
    curr_time = now_ist.time()
    date_str = curr_date.strftime("%Y-%m-%d")

    # Weekend check
    if now_ist.weekday() >= 5:
        return False, "Weekend (Saturday/Sunday) - MCX Closed"

    # Full Day MCX Holiday
    if date_str in MCX_FULL_DAY_HOLIDAYS:
        return False, f"MCX Full-Day Holiday ({MCX_FULL_DAY_HOLIDAYS[date_str]})"

    # Morning session closed holiday (e.g. Diwali Balipratipada)
    if date_str in MCX_MORNING_CLOSED_HOLIDAYS:
        if curr_time < datetime.time(17, 0):
            return False, f"MCX Morning Closed ({MCX_MORNING_CLOSED_HOLIDAYS[date_str]} - Evening opens at 17:00 IST)"
        elif curr_time > datetime.time(23, 30):
            return False, "MCX Evening Session Ended (23:30 IST)"
        return True, f"MCX Evening Session Active ({MCX_MORNING_CLOSED_HOLIDAYS[date_str]})"

    # Regular session
    if not (datetime.time(9, 0) <= curr_time <= datetime.time(23, 30)):
        return False, f"MCX Off-Hours (Current: {curr_time.strftime('%H:%M:%S')}, Hours: 09:00 - 23:30 IST)"

    return True, "MCX Market Open"


def is_candle_fresh_today(candle_timestamp: Any, max_stale_hours: float = 24.0) -> bool:
    """
    Verifies that a candle timestamp belongs to TODAY's trading session.
    Guards against Yahoo Finance serving yesterday's stale candles during holidays.
    """
    now_ist = get_current_ist_datetime()
    try:
        if isinstance(candle_timestamp, str):
            ts = pd.to_datetime(candle_timestamp)
        else:
            ts = candle_timestamp

        # Convert to date
        if hasattr(ts, 'date'):
            c_date = ts.date()
        else:
            c_date = pd.to_datetime(ts).date()

        # Candle must be generated TODAY
        return c_date == now_ist.date()
    except Exception:
        return False


def check_calendar_coverage_next_n_trading_days(n: int = 10) -> Dict[str, Any]:
    """
    Verifies that the holiday calendar accurately covers the next N trading days from today.
    Returns status: bool, trading_days: list, and covered: bool.
    """
    now_ist = get_current_ist_datetime()
    curr_date = now_ist.date()
    trading_days = []
    checked_days = 0
    test_date = curr_date
    
    # Check forward until we identify n trading days
    while len(trading_days) < n and checked_days < 60:
        checked_days += 1
        test_date += datetime.timedelta(days=1)
        if test_date.weekday() >= 5: # Saturday / Sunday
            continue
        is_hol, _ = is_nse_holiday(test_date)
        if not is_hol:
            trading_days.append(test_date.strftime("%Y-%m-%d"))

    # Determine if current configured calendar contains definitions covering the range
    max_cal_year = max([int(d.split("-")[0]) for d in NSE_TRADING_HOLIDAYS.keys()] or [2026])
    last_needed_year = int(trading_days[-1].split("-")[0]) if trading_days else curr_date.year
    covered = (max_cal_year >= last_needed_year) and len(trading_days) >= n

    return {
        "covered": covered,
        "required_trading_days": n,
        "found_trading_days_count": len(trading_days),
        "next_trading_days": trading_days,
        "latest_holiday_calendar_year": max_cal_year
    }


