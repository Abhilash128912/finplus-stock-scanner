"""
FinPlus+ Market Data Feed
Ingests historical 15m candles and real-time quotes across two strictly decoupled universes:
1. Stock Options Universe (FNOContractMaster): F&O equities with lot size <= 500 (+ Reliance) & Indices.
2. Intraday Cash Universe (BroadMarketMaster): Quality, high-liquidity, anti-circuit equities loaded from 'D:\\Nifty 500 stocks.xlsx'.
"""

import os
import time
import pandas as pd
from typing import Dict, Any, List, Optional, Set
from datetime import datetime, timezone

from src.feeds.fno_contract_master import FNOContractMaster
from src.feeds.broad_market_master import BroadMarketMaster
from src.utils.time_and_logger import setup_logger, get_utc_iso
from src.gateway.yfinance_gateway import YFinanceGateway
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
        self.gateway = YFinanceGateway()
        self.candle_store = CandleStore()
        self.last_fallback_batch_time: float = 0.0
        self.fallback_batch_min_interval: float = 120.0 # Max 1 batch per 2 minutes
        self.legacy_yahoo_scan_path_enabled: bool = os.getenv("ENABLE_LEGACY_YAHOO_SCAN_PATH", "true").lower() == "true"
        
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
        """Deprecated direct handle. Ticker queries should route via YFinanceGateway."""
        return None

    def prefetch_batch_15m_candles(self, tickers: List[str], period: str = "5d", chunk_size: int = 35) -> int:
        """
        During market hours: Yahoo prefetching is completely DISABLED (0 Yahoo calls).
        Closed 15m bars are loaded directly from CandleStore and in-memory cache.
        Outside market hours: Used only by scheduled off-hours backfill runner with HISTORY_BACKFILL.
        """
        now = time.time()
        # In market hours: If safety valve is OFF, strictly 0 Yahoo calls. Populate cache from CandleStore!
        # If safety valve is ON (coverage < 90%), legacy scan path remains available to prevent starved scanner.
        if is_equity_market_open_ist() and not self.legacy_yahoo_scan_path_enabled:
            loaded = 0
            for ticker in tickers:
                clean = ticker.replace(".NS", "")
                if ticker not in self._cache or (now - self._cache[ticker]['ts']) >= self.cache_ttl:
                    df = self.candle_store.get_candles_as_dataframe(clean, "15m", limit=1000)
                    if not df.empty:
                        self._cache[ticker] = {'df': df, 'ts': now}
                        loaded += 1
            return loaded

        # Off-hours backfill logic routed through gateway with HISTORY_BACKFILL
        needed = [t for t in tickers if t and (t not in self._cache or (now - self._cache[t]['ts']) >= self.cache_ttl)]
        if not needed:
            return 0

        logger.info(f"[HISTORY_BACKFILL] Prefetching batch candles for {len(needed)} tickers (chunks of {chunk_size})...")
        downloaded = 0
        for i in range(0, len(needed), chunk_size):
            chunk = needed[i:i + chunk_size]
            raw_df = None
            try:
                raw_df = self.gateway.download(chunk, reason="HISTORY_BACKFILL", period=period, interval="15m", group_by="ticker", threads=True)
            except Exception as ex:
                logger.warning(f"Gateway download error for chunk {chunk}: {ex}")

            if raw_df is not None and not raw_df.empty:
                if len(chunk) == 1:
                    t = chunk[0]
                    df = raw_df.copy()
                    if isinstance(df.columns, pd.MultiIndex):
                        df.columns = df.columns.get_level_values(0)
                    self._cache[t] = {'df': df, 'ts': now}
                    self.candle_store.save_dataframe_candles(t.replace(".NS", ""), "15m", df)
                    downloaded += 1
                else:
                    for t in chunk:
                        try:
                            if hasattr(raw_df.columns, "levels") and t in raw_df.columns.levels[0]:
                                df = raw_df[t].dropna(how="all").copy()
                                if not df.empty:
                                    self._cache[t] = {'df': df, 'ts': now}
                                    self.candle_store.save_dataframe_candles(t.replace(".NS", ""), "15m", df)
                                    downloaded += 1
                        except Exception:
                            pass
            else:
                # Gateway failed or 429: fallback to CandleStore
                for t in chunk:
                    clean = t.replace(".NS", "")
                    local_df = self.candle_store.get_candles_as_dataframe(clean, "15m")
                    if not local_df.empty:
                        self._cache[t] = {'df': local_df, 'ts': now}
                        self.fallback_tickers_this_cycle.add(t)
                        downloaded += 1
        return downloaded

    def fetch_15m_candles(self, ticker: str, period: str = "5d", allow_fallback_guard: bool = False, broker_stale_seconds: float = 0.0, is_priority_symbol: bool = False) -> pd.DataFrame:
        """
        INDmoney / CandleStore FIRST data resolution:
        1. Checks in-memory cache.
        2. Checks SQLite CandleStore.
        3. Only contacts gateway (FALLBACK_GUARD) if broker is stale > 3 minutes (180s)
           AND symbol has an open position or is in top-3 candidates.
        4. Otherwise returns local data tagged appropriately.
        """
        now = time.time()
        if ticker in self._cache:
            entry = self._cache[ticker]
            if (now - entry['ts']) < self.cache_ttl:
                return entry['df']

        clean_sym = ticker.replace(".NS", "")
        # Step 1: Check CandleStore
        local_df = self.candle_store.get_candles_as_dataframe(clean_sym, "15m", limit=600)
        if not local_df.empty and not allow_fallback_guard:
            self._cache[ticker] = {'df': local_df, 'ts': now}
            return local_df

        # Step 2: Fallback Guard check (broker stale > 180s AND priority symbol)
        # 2e Requirement: CandleStore has no bar newer than 5 minutes for that symbol, max 1 batch per 2 mins
        if allow_fallback_guard and broker_stale_seconds > 180.0 and is_priority_symbol:
            latest_bar_epoch = self.candle_store.get_latest_candle_timestamp(clean_sym, "15m")
            bar_age_sec = (now - latest_bar_epoch) if latest_bar_epoch else 9999.0
            
            # Rate limit fallback calls to max 1 batch per 2 minutes (120s)
            time_since_last_fb = now - self.last_fallback_batch_time
            if bar_age_sec > 300.0 and (time_since_last_fb >= self.fallback_batch_min_interval or self.last_fallback_batch_time == 0.0):
                self.last_fallback_batch_time = now
                logger.warning(
                    f"FALLBACK_GUARD ACTIVATED for {ticker}: Broker stale {int(broker_stale_seconds)}s (>180s), "
                    f"latest CandleStore bar is {int(bar_age_sec)}s old (>300s). Querying gateway..."
                )
                df = self.gateway.download(ticker, reason="FALLBACK_GUARD", caller="MarketDataFeed", trigger_detail="Broker stale > 3m & CandleStore bar > 5m", period=period, interval="15m")
                if not df.empty:
                    if isinstance(df.columns, pd.MultiIndex):
                        df.columns = df.columns.get_level_values(0)
                    df.attrs["data_source"] = "YF_FALLBACK"
                    self._cache[ticker] = {'df': df, 'ts': now}
                    self.fallback_tickers_this_cycle.add(ticker)
                    try:
                        self.candle_store.save_dataframe_candles(clean_sym, "15m", df)
                    except Exception:
                        pass
                    return df

        # If not falling back to Yahoo, return local_df (or empty)
        if not local_df.empty:
            self._cache[ticker] = {'df': local_df, 'ts': now}
            return local_df

        return pd.DataFrame()
