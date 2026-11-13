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
        top_n_fno_stocks: int = 150,
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
        self.fallback_tickers_this_cycle: set = set()
        self.last_fallback_alert_time: float = 0.0
        
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

    def prefetch_batch_15m_candles(self, tickers: List[str], period: str = "5d", chunk_size: int = 35) -> int:
        """
        Batch downloads 15m candles in chunks of 35 tickers using multi-threaded yfinance.
        Populates self._cache in ~25s for the entire universe and eliminates rate limits.
        """
        now = time.time()
        needed = [t for t in tickers if t and (t not in self._cache or (now - self._cache[t]['ts']) >= self.cache_ttl)]
        if not needed:
            return 0

        logger.info(f"Prefetching batch candles for {len(needed)} tickers (chunks of {chunk_size})...")
        downloaded = 0

        for i in range(0, len(needed), chunk_size):
            chunk = needed[i:i + chunk_size]
            raw_df = None
            max_retries = 2
            backoffs = [2.0, 6.0]

            for attempt in range(max_retries + 1):
                try:
                    raw_df = yf.download(chunk, period=period, interval="15m", group_by="ticker", progress=False, threads=True)
                    if raw_df is not None and not raw_df.empty:
                        break
                except Exception as ex:
                    if attempt < max_retries:
                        sleep_s = backoffs[attempt]
                        logger.warning(f"Batch chunk failed ({ex}). Retrying in {sleep_s}s (Attempt {attempt + 1}/{max_retries})...")
                        time.sleep(sleep_s)
                    else:
                        logger.error(f"Batch candle chunk failed after {max_retries} retries: {ex}")

            from src.storage.candle_store import CandleStore
            cs = CandleStore()

            if raw_df is not None and not raw_df.empty:
                if len(chunk) == 1:
                    ticker = chunk[0]
                    df = raw_df.copy()
                    if isinstance(df.columns, pd.MultiIndex):
                        df.columns = df.columns.get_level_values(0)
                    self._cache[ticker] = {'df': df, 'ts': now}
                    try:
                        cs.save_dataframe_candles(ticker.replace(".NS", ""), "15m", df)
                    except Exception:
                        pass
                    downloaded += 1
                else:
                    for ticker in chunk:
                        try:
                            if hasattr(raw_df.columns, "levels") and ticker in raw_df.columns.levels[0]:
                                df = raw_df[ticker].dropna(how="all").copy()
                                if not df.empty:
                                    self._cache[ticker] = {'df': df, 'ts': now}
                                    try:
                                        cs.save_dataframe_candles(ticker.replace(".NS", ""), "15m", df)
                                    except Exception:
                                        pass
                                    downloaded += 1
                        except Exception:
                            pass
            else:
                # Yahoo failed after retries: fallback to CandleStore for tickers in chunk
                for ticker in chunk:
                    clean = ticker.replace(".NS", "")
                    local_df = cs.get_candles_as_dataframe(clean, "15m")
                    if not local_df.empty:
                        self._cache[ticker] = {'df': local_df, 'ts': now}
                        self.fallback_tickers_this_cycle.add(ticker)
                        logger.warning(f"[FALLBACK_CANDLESTORE] Yahoo unavailable for {ticker}. Serving {len(local_df)} bars from CandleStore.")
                        downloaded += 1

        logger.info(f"Prefetch complete: {downloaded}/{len(needed)} tickers updated in cache and persistent candle store.")
        return downloaded

    def fetch_15m_candles(self, ticker: str, period: str = "5d") -> pd.DataFrame:
        """Fetches 15-minute intraday bars with in-memory caching and persistent DB fallback."""
        now = time.time()
        if ticker in self._cache:
            entry = self._cache[ticker]
            if (now - entry['ts']) < self.cache_ttl:
                return entry['df']
                
        clean_sym = ticker.replace(".NS", "")
        from src.storage.candle_store import CandleStore
        cs = CandleStore()

        try:
            logger.info(f"Downloading 15m candles for {ticker} (Period: {period})...")
            df = yf.download(ticker, period=period, interval="15m", progress=False)
            if df.empty:
                logger.warning(f"Empty data returned for {ticker}. Checking local candle store...")
                local_df = cs.get_candles_as_dataframe(clean_sym, "15m")
                if not local_df.empty:
                    self._cache[ticker] = {'df': local_df, 'ts': now}
                    return local_df
                return pd.DataFrame()
                
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
                
            self._cache[ticker] = {
                'df': df,
                'ts': now
            }
            try:
                cs.save_dataframe_candles(clean_sym, "15m", df)
            except Exception:
                pass
            return df
        except Exception as e:
            logger.error(f"Error downloading {ticker}: {e}. Falling back to local candle store...")
            local_df = cs.get_candles_as_dataframe(clean_sym, "15m")
            if not local_df.empty:
                self._cache[ticker] = {'df': local_df, 'ts': now}
                return local_df
            if ticker in self._cache:
                return self._cache[ticker]['df']
            return pd.DataFrame()
