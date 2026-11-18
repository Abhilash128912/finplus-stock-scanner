"""
FinPlus+ Market Data Feed
Ingests historical 15m candles and real-time quotes across two strictly decoupled universes:
1. Stock Options Universe (FNOContractMaster): F&O equities with lot size <= 500 (+ Reliance) & Indices.
2. Intraday Cash Universe (BroadMarketMaster): Quality, high-liquidity, anti-circuit equities loaded from 'D:\\Nifty 500 stocks.xlsx'.
100% Routed through INDstocks Live Feed & Local CandleStore. Zero third-party dependencies.
"""

import os
import time
import pandas as pd
from typing import Dict, Any, List, Optional, Set
from datetime import datetime, timezone

from src.feeds.fno_contract_master import FNOContractMaster
from src.feeds.broad_market_master import BroadMarketMaster
from src.utils.time_and_logger import setup_logger, get_utc_iso
from src.paper.indmoney_live_feed import INDMoneyLiveFeed
from src.storage.candle_store import CandleStore
from src.utils.indian_market_calendar import is_equity_market_open_ist

logger = setup_logger("MarketDataFeed")


class MarketDataFeed:
    def __init__(
        self,
        cache_ttl_seconds: int = 60,
        max_lot_size: int = 500,
        top_n_fno_stocks: int = 150,
        top_n_cash_stocks: int = 750,
    ):
        self.cache_ttl = cache_ttl_seconds
        self.indmoney_feed = INDMoneyLiveFeed()
        self.candle_store = CandleStore()
        
        # 1. Stock Options Universe (F&O Lot <= 500 + Reliance + Sensex/Nifty)
        self.fno_master = FNOContractMaster(max_lot_size=max_lot_size)
        self.fno_universe = self.fno_master.load_universe(top_n_equities=top_n_fno_stocks)
        
        # 2. Intraday Cash Universe (Broad Market Quality & Anti-Circuit from Excel)
        self.broad_cash_master = BroadMarketMaster()
        self.cash_universe = self.broad_cash_master.load_universe(top_n_liquid=top_n_cash_stocks)
        
        # Combined tracked list with clear categories
        self.universe: List[Dict[str, Any]] = self.fno_universe + self.cash_universe
        self._cache = {}
        self.fallback_tickers_this_cycle: set = set()
        
        logger.info(
            f"MarketDataFeed initialized: {len(self.fno_universe)} F&O/Index Option instruments "
            f"+ {len(self.cash_universe)} Intraday Cash stocks = {len(self.universe)} total tracked. "
            f"Quotes routed via INDstocks Live Feed."
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

    def fetch_live_quotes(self, symbols: List[str], force: bool = False) -> Dict[str, Dict[str, Any]]:
        """Routes real-time quotes directly through official INDstocks broker feed."""
        return self.indmoney_feed.fetch_batch_quotes(symbols, force=force)

    def get_live_quote(self, symbol: str) -> Optional[float]:
        """Returns live LTP for a single symbol from INDstocks."""
        return self.indmoney_feed.get_single_quote(symbol)

    def prefetch_batch_15m_candles(self, tickers: List[str], period: str = "5d", chunk_size: int = 35) -> int:
        """
        Loads 15m closed bars directly from local CandleStore into in-memory cache.
        Zero external network calls.
        """
        now = time.time()
        loaded = 0
        for ticker in tickers:
            clean = ticker.replace(".NS", "").split(" ")[0].upper()
            if ticker not in self._cache or (now - self._cache[ticker]['ts']) >= self.cache_ttl:
                df = self.candle_store.get_candles_as_dataframe(clean, "15m", limit=1000)
                if not df.empty:
                    self._cache[ticker] = {'df': df, 'ts': now}
                    loaded += 1
        return loaded

    def fetch_15m_candles(
        self,
        ticker: str,
        period: str = "5d",
        allow_fallback_guard: bool = False,
        broker_stale_seconds: float = 0.0,
        is_priority_symbol: bool = False
    ) -> pd.DataFrame:
        """
        Retrieves 15m candles from in-memory cache or local SQLite CandleStore.
        Strict INDstocks / CandleStore resolution only.
        """
        now = time.time()
        if ticker in self._cache:
            entry = self._cache[ticker]
            if (now - entry['ts']) < self.cache_ttl:
                return entry['df']

        clean_sym = ticker.replace(".NS", "").split(" ")[0].upper()
        local_df = self.candle_store.get_candles_as_dataframe(clean_sym, "15m", limit=600)
        if not local_df.empty:
            self._cache[ticker] = {'df': local_df, 'ts': now}
            return local_df

        return pd.DataFrame()
