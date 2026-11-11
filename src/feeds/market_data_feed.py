"""
FinPlus+ Market Data Feed
Ingests historical 15m candles and real-time quotes across two strictly decoupled universes:
1. Stock Options Universe (FNOContractMaster): F&O equities with lot size <= 500 (+ Reliance) & Indices.
2. Intraday Cash Universe (BroadMarketMaster): Quality, high-liquidity, anti-circuit equities loaded from 'D:\\Nifty 500 stocks.xlsx'.
"""

import time
import pandas as pd
import yfinance as yf
from typing import Dict, Any, List, Optional
from datetime import datetime, timezone

from src.feeds.fno_contract_master import FNOContractMaster
from src.feeds.broad_market_master import BroadMarketMaster
from src.utils.time_and_logger import setup_logger, get_utc_iso

logger = setup_logger("MarketDataFeed")


class MarketDataFeed:
    def __init__(
        self,
        cache_ttl_seconds: int = 60,
        max_lot_size: int = 500,
        top_n_fno_stocks: int = 50,
        top_n_cash_stocks: int = 750,
    ):
        self.cache_ttl = cache_ttl_seconds
        
        # 1. Stock Options Universe (F&O Lot <= 500 + Reliance + Sensex/Nifty)
        self.fno_master = FNOContractMaster(max_lot_size=max_lot_size)
        self.fno_universe = self.fno_master.load_universe(top_n_equities=top_n_fno_stocks)
        
        # 2. Intraday Cash Universe (Broad Market Quality & Anti-Circuit from Excel)
        self.broad_cash_master = BroadMarketMaster()
        self.cash_universe = self.broad_cash_master.load_universe(top_n_liquid=top_n_cash_stocks)
        
        # Combined tracked list with clear categories
        self.universe: List[Dict[str, Any]] = self.fno_universe + self.cash_universe
        self._cache = {}
        
        logger.info(
            f"MarketDataFeed initialized: {len(self.fno_universe)} F&O/Index Option instruments "
            f"+ {len(self.cash_universe)} Intraday Cash stocks = {len(self.universe)} total tracked."
        )

    def get_fno_universe(self) -> List[Dict[str, Any]]:
        """Returns only Option instruments (Sensex, Nifty, and liquid F&O stocks)."""
        return self.fno_universe

    def get_cash_universe(self) -> List[Dict[str, Any]]:
        """Returns only Intraday Cash instruments (from Broad Market 750/500 list)."""
        return self.cash_universe

    def get_all_universe_items(self) -> List[Dict[str, Any]]:
        """Returns full universe for scanning."""
        return self.universe

    def _get_ticker(self, ticker: str):
        """Returns yfinance Ticker object with cached ticker handle."""
        return yf.Ticker(ticker)

    def fetch_15m_candles(self, ticker: str, period: str = "5d") -> pd.DataFrame:
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
