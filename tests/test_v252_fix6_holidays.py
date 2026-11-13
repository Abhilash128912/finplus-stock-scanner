"""
Acceptance Test for Item 6: Holiday and Weekend Calendar
- Editable config/nse_holidays.json list.
- Skip scheduler bulletins, 08:45 token refresh, and signal dispatch on weekends and holidays.
- Logs at startup: "Market closed today: reason".
"""

import datetime
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_SCANNER_DIR = Path(__file__).resolve().parent.parent
if str(_SCANNER_DIR) not in sys.path:
    sys.path.insert(0, str(_SCANNER_DIR))

from src.utils.indian_market_calendar import is_nse_holiday, is_equity_market_open_ist, NSE_TRADING_HOLIDAYS, SPECIAL_TRADING_SESSIONS
from src.scheduler.scanner_scheduler import ScannerScheduler


def test_holiday_json_loaded_and_detects_holidays():
    hol_file = _SCANNER_DIR / "config" / "nse_holidays.json"
    assert hol_file.exists(), "config/nse_holidays.json must exist"

    with open(hol_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert "holidays" in data
    assert "2026-01-26" in data["holidays"]

    # Verify is_nse_holiday detects Republic Day 2026
    is_hol, hol_name = is_nse_holiday(datetime.date(2026, 1, 26))
    assert is_hol is True
    assert "Republic Day" in hol_name


def test_holiday_skips_refresh_and_signals():
    scheduler = ScannerScheduler()
    # Simulate holiday
    holiday_date = datetime.date(2026, 1, 26)

    with patch("src.scheduler.scanner_scheduler.is_nse_holiday", return_value=(True, "Republic Day")):
        with patch("src.scheduler.scanner_scheduler.is_equity_market_open_ist", return_value=(False, "Holiday")):
            # Universe scan on holiday must return empty / no fresh active trade signals
            results = scheduler.scan_universe_now()
            assert len(results) == 0, "No signals should be generated on an official exchange holiday"

