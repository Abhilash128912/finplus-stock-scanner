"""
Acceptance Test for MarketDataFeed CandleStore Loading
- Populates cache directly from CandleStore bars.
- Zero external calls.
"""

import sys
import time
from pathlib import Path
import pandas as pd
import pytest

_SCANNER_DIR = Path(__file__).resolve().parent.parent
if str(_SCANNER_DIR) not in sys.path:
    sys.path.insert(0, str(_SCANNER_DIR))

from src.feeds.market_data_feed import MarketDataFeed
from src.storage.candle_store import CandleStore


def test_market_data_feed_loads_from_candlestore(tmp_path):
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
    feed.candle_store = cs
    feed._cache.clear()

    downloaded = feed.prefetch_batch_15m_candles(["RELIANCE.NS"], chunk_size=1)
    assert downloaded == 1

    # CandleStore must have populated the cache
    assert "RELIANCE.NS" in feed._cache
    assert len(feed._cache["RELIANCE.NS"]["df"]) == 2
