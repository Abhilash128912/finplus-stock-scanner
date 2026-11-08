"""
FinPlus+ Market Data Feed
Ingests historical 15m candles and real-time live quotes for dynamic F&O instruments
loaded via FNOContractMaster.
"""

import time
import pandas as pd
import yfinance as yf
from typing import Dict, Any, List, Optional
from datetime import datetime, timezone

from src.feeds.fno_contract_master import FNOContractMaster
from src.utils.time_and_logger import setup_logger, get_utc_iso

logger = setup_logger("MarketDataFeed")

class MarketDataFeed:
    def __init__(self, cache_ttl_seconds: int = 60, max_lot_size: int = 500, top_n_stocks: int = 25):
        self.cache_ttl = cache_ttl_seconds
        self.master = FNOContractMaster(max_lot_size=max_lot_size)
        self.universe = self.master.load_universe(top_n_equities=top_n_stocks)
        self._cache = {}

    def get_all_universe_items(self) -> List[Dict[str, Any]]:
        return self.universe

    def fetch_15m_candles(self, ticker: str, period: str = "59d") -> pd.DataFrame:
        """Fetches 15-minute intraday bars with in-memory caching."""
        now = time.time()
        if ticker in self._cache:
            entry = self._cache[ticker]
            if (now - entry['ts']) < self.cache_ttl:
                return entry['df']
                
        try:
            logger.info(f"Downloading 15m candles for {ticker} (Period: {period})...")
            df = yf.download(ticker, period=period, interval="15m", progress=False)
            if df.empty:
                logger.warning(f"Empty data returned for {ticker}")
                return pd.DataFrame()
                
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
                
            self._cache[ticker] = {
                'df': df,
                'ts': now
            }
            return df
        except Exception as e:
            logger.error(f"Error downloading {ticker}: {e}")
            if ticker in self._cache:
                return self._cache[ticker]['df']
            return pd.DataFrame()
