"""
FinPlus+ INDstocks Official Broker Live Market Feed
Connects directly to https://api.indstocks.com using user credentials and TOTP.
Provides 0-delay real-time official exchange quotes for:
- All MCX Commodities (Crude Oil Mini, Natural Gas Mini, Gold Petal)
- All NSE Equities and F&O underlying stocks
"""

from __future__ import annotations

import os
import sys
import time
import requests
from typing import Dict, Any, List, Optional
from pathlib import Path

# Ensure root paths are in sys.path
_CURRENT_DIR = Path(__file__).resolve().parent
_SCANNER_DIR = _CURRENT_DIR.parent.parent
_APPS_ROOT = _SCANNER_DIR.parent

for p in [str(_APPS_ROOT), str(_SCANNER_DIR)]:
    if p not in sys.path:
        sys.path.insert(0, p)

from src.utils.time_and_logger import setup_logger

logger = setup_logger("INDMoneyLiveFeed")

try:
    from finplus_core.auth import INDMoneyAuthClient
    AUTH_CLIENT_AVAILABLE = True
except Exception as e:
    logger.warning(f"Could not import INDMoneyAuthClient: {e}")
    AUTH_CLIENT_AVAILABLE = False


# Known Official Scrip Codes for Commodities & Active F&O Equities
DEFAULT_SCRIP_REGISTRY = {
    # MCX Commodities (Official Security IDs on INDstocks)
    "CRUDEOILM": "MCX_573423",
    "NATGASMINI": "MCX_574320",
    "GOLDPETAL": "MCX_574841",
    
    # Common F&O Stocks on NSE
    "COFORGE": "NSE_11543",
    "ADANIPORTS": "NSE_15083",
    "HCLTECH": "NSE_7229",
    "GRAPHITE": "NSE_592",
    "CUMMINSIND": "NSE_1901",
    "HDFCBANK": "NSE_1333",
    "RELIANCE": "NSE_2885",
    "ICICIBANK": "NSE_4963",
    "INFY": "NSE_1594",
    "TCS": "NSE_11536",
    "LT": "NSE_11483",
    "BHARTIARTL": "NSE_10604",
    "AXISBANK": "NSE_5900",
    "KOTAKBANK": "NSE_1922",
    "SBIN": "NSE_3045",
    "MARUTI": "NSE_10999",
    "TATAMOTORS": "NSE_3456",
    "TATASTEEL": "NSE_3499",
    "BAJFINANCE": "NSE_317",
    "CIPLA": "NSE_694",
    "BOSCHLTD": "NSE_2181",
    "CGPOWER": "NSE_7083",
    "MFSL": "NSE_2142",
    "APLAPOLLO": "NSE_14486",
    "BSE": "NSE_19585",
    "CRAFTSMAN": "NSE_2139",
    "MCX": "NSE_31181",
    "SONACOMS": "NSE_2868"
}


class INDMoneyLiveFeed:
    """Manages real-time batch quote retrieval directly from INDmoney broker API."""

    BASE_URL = "https://api.indstocks.com"
    QUOTES_ENDPOINT = "/market/quotes/full"

    def __init__(self, cache_ttl_seconds: float = 2.0):
        self.cache_ttl = cache_ttl_seconds
        self.auth_client = None
        self._quote_cache: Dict[str, Dict[str, Any]] = {}
        self._last_fetch_time: float = 0.0
        self._broker_cooldown_until: float = 0.0
        self.scrip_registry = dict(DEFAULT_SCRIP_REGISTRY)
        self._load_equity_instruments_registry()

        if AUTH_CLIENT_AVAILABLE:
            try:
                env_candidates = [
                    os.getenv("INDMONEY_ENV_PATH"),
                    str(_APPS_ROOT / "config" / ".env"),
                    str(_APPS_ROOT / "finplus_index_breadth" / "config" / ".env"),
                    str(_SCANNER_DIR / "config" / ".env"),
                ]
                chosen_env = None
                for c in env_candidates:
                    if c and os.path.exists(c):
                        chosen_env = c
                        break

                if chosen_env:
                    self.auth_client = INDMoneyAuthClient(env_file_path=chosen_env)
                else:
                    self.auth_client = INDMoneyAuthClient()
                logger.info("INDMoneyLiveFeed initialized with active INDMoneyAuthClient.")
            except Exception as e:
                logger.warning(f"Failed to initialize INDMoneyAuthClient: {e}")

    def _load_equity_instruments_registry(self) -> None:
        """Loads official NSE security codes from equity_instruments.csv if available."""
        csv_candidates = [
            _SCANNER_DIR / "equity_instruments.csv",
            _APPS_ROOT / "finplus_stock_scanner" / "equity_instruments.csv",
            _APPS_ROOT / "config" / "equity_instruments.csv"
        ]
        for csv_path in csv_candidates:
            if csv_path.exists():
                try:
                    import csv
                    count = 0
                    with open(csv_path, mode="r", encoding="utf-8", errors="ignore") as f:
                        reader = csv.DictReader(f)
                        for row in reader:
                            if row.get("EXCH") == "NSE" and row.get("SEGMENT") == "E" and row.get("SERIES") == "EQ":
                                sym = row.get("TRADING_SYMBOL", "").strip().upper()
                                sec_id = row.get("SECURITY_ID", "").strip()
                                if sym and sec_id and sym not in self.scrip_registry:
                                    self.scrip_registry[sym] = f"NSE_{sec_id}"
                                    count += 1
                    logger.info(f"Loaded {count} additional NSE scrip codes from {csv_path.name} (Total: {len(self.scrip_registry)}).")
                    return
                except Exception as e:
                    logger.warning(f"Could not parse {csv_path}: {e}")

    def resolve_scrip_code(self, symbol: str) -> Optional[str]:
        """Resolves symbol to INDstocks scrip-code (e.g. CRUDEOILM -> MCX_573423, COFORGE -> NSE_11543)."""
        clean = symbol.split(" ")[0].upper()
        return self.scrip_registry.get(clean)

    def fetch_batch_quotes(self, symbols: List[str], force: bool = False) -> Dict[str, Dict[str, Any]]:
        """
        Fetches live quotes for multiple symbols simultaneously in 1 single HTTP request.
        Returns mapping of clean symbol -> quote info (ltp, change_pct, day_high, day_low, volume).
        """
        if not self.auth_client:
            return {}

        now = time.time()
        if not force and (now - self._last_fetch_time) < self.cache_ttl and self._quote_cache:
            return self._quote_cache

        if not force and now < self._broker_cooldown_until:
            return self._quote_cache

        # Map symbols to scrips
        scrip_to_sym = {}
        for s in symbols:
            clean = s.split(" ")[0].upper()
            scrip = self.resolve_scrip_code(clean)
            if scrip:
                scrip_to_sym[scrip] = clean

        if not scrip_to_sym:
            return {}

        try:
            headers = self.auth_client.get_auth_headers()
        except Exception as e:
            logger.warning(f"Failed to obtain INDstocks auth headers ({e}). Setting 60s cooldown.")
            self._broker_cooldown_until = now + 60.0
            return self._quote_cache

        scrip_list = list(scrip_to_sym.keys())
        results = {}
        chunk_size = 15

        for i in range(0, len(scrip_list), chunk_size):
            chunk = scrip_list[i:i + chunk_size]
            joined = ",".join(chunk)
            url = f"{self.BASE_URL}{self.QUOTES_ENDPOINT}?scrip-codes={joined}"

            try:
                resp = requests.get(url, headers=headers, timeout=3.0)
                if resp.status_code == 200:
                    payload = resp.json()
                    data = payload.get("data", {})
                    for scrip, raw in data.items():
                        sym = scrip_to_sym.get(scrip)
                        if sym and raw:
                            ltp = float(raw.get("live_price", 0.0))
                            if ltp > 0:
                                results[sym] = {
                                    "symbol": sym,
                                    "scrip_code": scrip,
                                    "ltp": ltp,
                                    "change_pct": float(raw.get("day_change_percentage", 0.0)),
                                    "day_high": float(raw.get("day_high", ltp)),
                                    "day_low": float(raw.get("day_low", ltp)),
                                    "day_open": float(raw.get("day_open", ltp)),
                                    "prev_close": float(raw.get("prev_close", ltp)),
                                    "volume": int(raw.get("volume", 0)),
                                    "source": "INDstocks Live Broker API"
                                }
                elif resp.status_code in (401, 403, 429):
                    logger.warning(f"INDstocks quote returned HTTP {resp.status_code}. Setting 60s cooldown.")
                    self._broker_cooldown_until = now + 60.0
                    break
            except Exception as e:
                logger.debug(f"INDstocks batch quote error for chunk {i}: {e}")

        if results:
            self._quote_cache.update(results)
            self._last_fetch_time = now
            return self._quote_cache

        return self._quote_cache

    def get_single_quote(self, symbol: str) -> Optional[float]:
        """Returns live LTP for a single symbol from INDmoney, or None if unavailable."""
        clean = symbol.split(" ")[0].upper()
        quotes = self.fetch_batch_quotes([clean])
        item = quotes.get(clean)
        if item:
            return item.get("ltp")
        return None
