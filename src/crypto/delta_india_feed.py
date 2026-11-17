"""
FinPlus+ Delta Exchange India Feed Client
Polls Delta Exchange India REST API for live tickers, orderbook depth, and OHLCV candles.
"""

import time
import requests
from typing import Dict, Any, List, Optional
from src.utils.time_and_logger import setup_logger
from src.crypto.crypto_config import DELTA_API_BASE

logger = setup_logger("DeltaIndiaFeed")

class DeltaIndiaFeed:
    def __init__(self, base_url: str = DELTA_API_BASE):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "FinPlus-Crypto-Radar/1.0",
            "Accept": "application/json"
        })
        self._ticker_cache: Dict[str, Dict[str, Any]] = {}
        self._ticker_cache_ts: float = 0.0
        self._candle_cache: Dict[str, Dict[str, Any]] = {}

    def get_all_tickers(self) -> Dict[str, Dict[str, Any]]:
        """
        Fetches all live tickers from Delta Exchange India with 10s caching.
        Returns a dict keyed by instrument symbol.
        """
        now = time.time()
        if self._ticker_cache and (now - self._ticker_cache_ts) < 10.0:
            return self._ticker_cache

        url = f"{self.base_url}/v2/tickers"
        try:
            resp = self.session.get(url, timeout=6.0)
            if resp.status_code == 200:
                data = resp.json()
                tickers_list = data.get("result", [])
                cache_map = {}
                for t in tickers_list:
                    sym = t.get("symbol")
                    if sym:
                        cache_map[sym] = t
                self._ticker_cache = cache_map
                self._ticker_cache_ts = now
                return self._ticker_cache
            else:
                logger.warning(f"Failed to fetch tickers: HTTP {resp.status_code}")
        except Exception as e:
            logger.error(f"Exception fetching tickers from Delta India: {e}")

        return self._ticker_cache

    def get_ticker(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Returns single ticker for a symbol, falling back to cached list."""
        all_t = self.get_all_tickers()
        if symbol in all_t:
            return all_t[symbol]

        url = f"{self.base_url}/v2/tickers/{symbol}"
        try:
            resp = self.session.get(url, timeout=5.0)
            if resp.status_code == 200:
                return resp.json().get("result")
        except Exception as e:
            logger.error(f"Error fetching ticker for {symbol}: {e}")
        return None

    def get_candles(self, symbol: str, resolution: str = "15m", count: int = 100) -> List[Dict[str, Any]]:
        """
        Fetches OHLCV historical candles for an instrument.
        Resolution can be '5m', '15m', '1h', '1d'.
        Returns list of candle dicts sorted chronologically (oldest -> newest).
        """
        cache_key = f"{symbol}_{resolution}"
        now = int(time.time())
        cached = self._candle_cache.get(cache_key)
        if cached and (now - cached["fetch_ts"]) < 20: # 20s TTL
            return cached["candles"]

        # Calculate start timestamp based on resolution and count
        res_seconds = {
            "1m": 60,
            "5m": 300,
            "15m": 900,
            "1h": 3600,
            "1d": 86400
        }.get(resolution, 900)

        start = now - (res_seconds * (count + 10))
        url = f"{self.base_url}/v2/history/candles?resolution={resolution}&symbol={symbol}&start={start}&end={now}"

        try:
            resp = self.session.get(url, timeout=8.0)
            if resp.status_code == 200:
                candles = resp.json().get("result", [])
                # Delta API returns newest first or oldest first? Let's ensure chronological sort by 'time'
                candles.sort(key=lambda c: c.get("time", 0))
                self._candle_cache[cache_key] = {
                    "fetch_ts": now,
                    "candles": candles
                }
                return candles
            else:
                logger.warning(f"Candles fetch for {symbol} ({resolution}) returned HTTP {resp.status_code}")
        except Exception as e:
            logger.error(f"Exception fetching candles for {symbol} ({resolution}): {e}")

        return cached["candles"] if cached else []
