"""
Acceptance Test for Item 3: Proactive Refresh + Stale Feed Guard
- 3a. Proactive refresh at 08:45 IST on trading days.
- 3b. If refresh fails or feed is >60s old, tag signal STALE_FEED, block new signals, and stop polling.
"""

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_SCANNER_DIR = Path(__file__).resolve().parent.parent
if str(_SCANNER_DIR) not in sys.path:
    sys.path.insert(0, str(_SCANNER_DIR))

from src.scheduler.scanner_scheduler import ScannerScheduler


def test_stale_feed_blocks_signals():
    """Requirement 3b: When feed is stale, candidates get STALE_FEED tag and signal_direction is 0."""
    scheduler = ScannerScheduler()
    scheduler.is_stale_feed = True

    # Mock feed returning candles with capitalized columns expected by RangeDetectorEngine
    import pandas as pd
    dummy_df = pd.DataFrame({
        "Open": [100.0] * 30,
        "High": [105.0] * 30,
        "Low": [95.0] * 30,
        "Close": [102.0] * 30,
        "Volume": [1000] * 30,
    })

    with patch.object(scheduler.feed, "fetch_15m_candles", return_value=dummy_df):
        with patch.object(scheduler.feed, "prefetch_batch_15m_candles"):
            with patch.object(scheduler.feed, "get_all_universe_items", return_value=[
                {"symbol": "TESTSYM", "yf_ticker": "TESTSYM.NS", "category": "INTRADAY_CASH"}
            ]):
                with patch("src.scheduler.scanner_scheduler.is_equity_market_open_ist", return_value=(True, "Market Open")):
                    results = scheduler.scan_universe_now()

    assert len(results) == 1
    item = results[0]
    assert item.get("feed_status") == "STALE_FEED"
    assert item.get("signal_direction") == 0
    assert "STALE_FEED" in item.get("regime_confluence", "")
