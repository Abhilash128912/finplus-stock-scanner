"""
Acceptance Test for Item 8: Yahoo Fallback & Exponential Backoff
- On failed/rate-limited chunk, retries up to 2 times with backoff (2s, 6s).
- Falls back to CandleStore bars for those tickers.
- Tags fallback tickers in scan log.
- If >10% of tickers fall back in one cycle, triggers NTFY alert (priority, max once/hour).
"""

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

_SCANNER_DIR = Path(__file__).resolve().parent.parent
if str(_SCANNER_DIR) not in sys.path:
    sys.path.insert(0, str(_SCANNER_DIR))

from src.feeds.market_data_feed import MarketDataFeed
from src.storage.candle_store import CandleStore


def test_yahoo_fallback_on_429_retries_and_uses_candlestore(tmp_path):
    cs_db = tmp_path / "test_cs.db"
    cs = CandleStore(db_path=str(cs_db))

    # Pre-populate CandleStore with bars for RELIANCE with distinct timestamps
    dummy_cs_df = pd.DataFrame(
        {
            "open": [2500.0, 2505.0],
            "high": [2510.0, 2515.0],
            "low": [2495.0, 2500.0],
            "close": [2505.0, 2510.0],
            "volume": [1000, 1500],
        },
        index=[pd.Timestamp("2026-11-13 09:15:00"), pd.Timestamp("2026-11-13 09:30:00")]
    )
    cs.save_dataframe_candles("RELIANCE", "15m", dummy_cs_df)

    feed = MarketDataFeed(max_lot_size=500, top_n_fno_stocks=5, top_n_cash_stocks=5)
    feed._cache.clear()

    retry_attempts = 0
    def mock_download_with_429(*args, **kwargs):
        nonlocal retry_attempts
        retry_attempts += 1
        raise Exception("HTTP 429 Too Many Requests (Rate Limited)")

    with patch("yfinance.download", side_effect=mock_download_with_429):
        with patch("time.sleep") as mock_sleep:
            with patch("src.storage.candle_store.CandleStore", return_value=cs):
                downloaded = feed.prefetch_batch_15m_candles(["RELIANCE.NS"], chunk_size=1)

    # 1 initial attempt + 2 retries = 3 attempts total
    assert retry_attempts == 3, f"Expected 3 download attempts (1 initial + 2 retries), got {retry_attempts}"
    assert mock_sleep.call_count == 2, "Expected 2 backoff sleep calls"

    # CandleStore fallback must have populated the cache
    assert "RELIANCE.NS" in feed._cache
    assert len(feed._cache["RELIANCE.NS"]["df"]) == 2
    assert "RELIANCE.NS" in feed.fallback_tickers_this_cycle
