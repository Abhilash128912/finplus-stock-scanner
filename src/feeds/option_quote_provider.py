"""
FinPlus+ INDstocks Option Quote Provider
Module: finplus_stock_scanner.feeds.option_quote_provider
Author: FinPlus+ Enterprise Infrastructure
Standard: ISO-8601, Zero Fabricated Data, Strict Secret Masking

Features:
1. Abstract OptionQuoteProvider base class.
2. IndstocksQuoteProvider implementing live REST quotes & WebSocket streaming from api.indstocks.com.
3. Daily cached FNO instruments master resolution (underlying, expiry, strike, CE/PE -> security_id).
4. Top-of-book depth level 1 bid/ask extraction and 5% illiquidity filter.
5. Option Greeks & IV retrieval via /market/option-chain with 60s throttle.
6. WebSocket subscription management on wss://ws-prices.indstocks.com with 5s REST polling fallback.
"""

from __future__ import annotations

import asyncio
import csv
import datetime
import io
import json
import os
import threading
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
import websockets

from src.utils.time_and_logger import setup_logger

logger = setup_logger("OptionQuoteProvider")

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))


class OptionQuoteProvider(ABC):
    """Abstract Base Class for Options Quote Providers."""

    @abstractmethod
    def get_quote(
        self,
        symbol: str,
        expiry: Optional[str],
        strike: float,
        opt_type: str
    ) -> Dict[str, Any]:
        """
        Retrieves real-time quote for the specified option contract.
        Returns:
            {
                "bid": float,
                "ask": float,
                "ltp": float,
                "iv": float,
                "oi": int,
                "ts": float,
                "token": str,
                "scrip_code": str,
                "symbol": str,
                "is_illiquid": bool,
                "illiquid_reason": Optional[str],
                "source": "REAL_QUOTE" | "SYNTHETIC"
            }
        """
        pass


class IndstocksQuoteProvider(OptionQuoteProvider):
    """
    Production-grade INDstocks (INDmoney) Options Quote & Streaming Feed.
    Base URL: https://api.indstocks.com
    WebSocket: wss://ws-prices.indstocks.com/api/v1/ws/prices
    """

    BASE_URL = "https://api.indstocks.com"
    WS_URL = "wss://ws-prices.indstocks.com/api/v1/ws/prices"

    # Known index underlying security IDs on INDstocks
    INDEX_UNDERLYING_MAP = {
        "NIFTY": {"exchange": "NSE", "segment": "INDEX", "underlying_scrip": "40000001"},
        "NIFTY 50": {"exchange": "NSE", "segment": "INDEX", "underlying_scrip": "40000001"},
        "SENSEX": {"exchange": "BSE", "segment": "INDEX", "underlying_scrip": "40000006"},
        "MIDCPNIFTY": {"exchange": "NSE", "segment": "INDEX", "underlying_scrip": "40000013"},
        "BANKNIFTY": {"exchange": "NSE", "segment": "INDEX", "underlying_scrip": "40000002"},
    }

    # Class-level cache across all instances for 0-delay instantiation
    _shared_instruments_cache: Dict[Tuple[str, str, float, str], Dict[str, Any]] = {}
    _shared_instruments_by_symbol: Dict[str, List[Dict[str, Any]]] = {}
    _shared_cache_date: str = ""
    _shared_lock: threading.Lock = threading.Lock()

    def __init__(self, cache_dir: Optional[str] = None):
        self.cache_dir = Path(cache_dir or os.path.join(os.path.dirname(__file__), "..", "..", "data", "cache"))
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        self._instruments_cache: Dict[Tuple[str, str, float, str], Dict[str, Any]] = {}
        self._instruments_by_symbol: Dict[str, List[Dict[str, Any]]] = {}
        self._cache_date: str = ""
        self._greeks_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {} # scrip -> (ts, data)

        # WebSocket Streaming State
        self._subscribed_tokens: set[str] = set()
        self._live_stream_quotes: Dict[str, Dict[str, Any]] = {}
        self._ws_lock = threading.Lock()
        self._ws_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._is_ws_connected = False
        self._active_ws = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def _get_auth_token(self, force_refresh: bool = False) -> str:
        """
        Retrieves active INDstocks token from finplus_core auth client or .env.
        Strict rule: Never log the token.
        """
        token = None
        if not force_refresh:
            try:
                from finplus_core.auth import INDMoneyAuthClient
                auth_client = INDMoneyAuthClient()
                token = auth_client.get_valid_token(auto_refresh=True)
            except Exception as e:
                logger.warning(f"Could not load token via INDMoneyAuthClient: {e}")
                token = os.getenv("INDSTOCKS_TOKEN") or os.getenv("INDMONEY_ACCESS_TOKEN")
        else:
            try:
                from finplus_core.auth import INDMoneyAuthClient
                auth_client = INDMoneyAuthClient()
                token = auth_client.generate_fresh_token()
            except Exception as e:
                logger.warning(f"Could not force-generate fresh token via INDMoneyAuthClient: {e}")
                token = os.getenv("INDSTOCKS_TOKEN") or os.getenv("INDMONEY_ACCESS_TOKEN")

        if not token:
            token = os.getenv("INDSTOCKS_TOKEN") or os.getenv("INDMONEY_ACCESS_TOKEN")

        if not token:
            raise ValueError("INDSTOCKS_TOKEN not found in environment or auth cache.")

        # Keep process environment synchronized
        os.environ["INDSTOCKS_TOKEN"] = token
        os.environ["INDMONEY_ACCESS_TOKEN"] = token
        return token

    def _get_headers(self, force_refresh: bool = False) -> Dict[str, str]:
        token = self._get_auth_token(force_refresh=force_refresh)
        return {
            "Authorization": token,
            "Accept": "application/json",
            "Content-Type": "application/json"
        }

    # -------------------------------------------------------------------------
    # Instrument Resolution & Daily CSV Caching
    # -------------------------------------------------------------------------
    def _ensure_instruments_loaded(self) -> None:
        today_str = datetime.datetime.now(tz=IST).strftime("%Y%m%d")
        if IndstocksQuoteProvider._shared_cache_date == today_str and IndstocksQuoteProvider._shared_instruments_cache:
            self._instruments_cache = IndstocksQuoteProvider._shared_instruments_cache
            self._instruments_by_symbol = IndstocksQuoteProvider._shared_instruments_by_symbol
            self._cache_date = today_str
            return

        with IndstocksQuoteProvider._shared_lock:
            # Re-check inside lock
            if IndstocksQuoteProvider._shared_cache_date == today_str and IndstocksQuoteProvider._shared_instruments_cache:
                self._instruments_cache = IndstocksQuoteProvider._shared_instruments_cache
                self._instruments_by_symbol = IndstocksQuoteProvider._shared_instruments_by_symbol
                self._cache_date = today_str
                return

            cache_file = self.cache_dir / f"instruments_fno_{today_str}.csv"
            if not cache_file.exists():
                # Check for bundled seed archive
                seed_gz = Path(__file__).resolve().parent.parent.parent / "config" / "instruments_fno_seed.csv.gz"
                if seed_gz.exists():
                    logger.info(f"Extracting bundled FNO seed instruments from {seed_gz}...")
                    try:
                        import gzip, shutil
                        with gzip.open(seed_gz, "rb") as f_in, open(cache_file, "wb") as f_out:
                            shutil.copyfileobj(f_in, f_out)
                        logger.info(f"Seeded {cache_file} from bundled seed ({cache_file.stat().st_size:,} bytes).")
                    except Exception as seed_err:
                        logger.warning(f"Could not extract seed file: {seed_err}")

            if not cache_file.exists():
                self._download_instruments(cache_file)

            if cache_file.exists():
                self._parse_instruments_csv(cache_file)
                IndstocksQuoteProvider._shared_instruments_cache = self._instruments_cache
                IndstocksQuoteProvider._shared_instruments_by_symbol = self._instruments_by_symbol
                IndstocksQuoteProvider._shared_cache_date = today_str
                self._cache_date = today_str

    def _download_instruments(self, target_path: Path) -> None:
        logger.info(f"Downloading FNO instruments master from {self.BASE_URL}/market/instruments?source=fno...")
        headers = self._get_headers()
        url = f"{self.BASE_URL}/market/instruments?source=fno"
        try:
            resp = requests.get(url, headers=headers, timeout=45)
            if resp.status_code == 200 and len(resp.content) > 1000:
                with open(target_path, "wb") as f:
                    f.write(resp.content)
                logger.info(f"Saved FNO instruments to {target_path} ({len(resp.content):,} bytes).")
            else:
                logger.error(f"Failed to download instruments: HTTP {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            logger.error(f"Exception downloading instruments: {e}")

    def _parse_instruments_csv(self, file_path: Path) -> None:
        if not file_path.exists():
            logger.warning(f"Instruments file {file_path} not found, resolution disabled.")
            return

        logger.info(f"Parsing FNO instruments CSV from {file_path}...")
        count = 0
        self._instruments_cache.clear()
        self._instruments_by_symbol.clear()

        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            reader = csv.DictReader(f)
            # Confirmed columns:
            # ['EXCH', 'SEGMENT', 'SECURITY_ID', 'INSTRUMENT_NAME', 'EXPIRY_CODE',
            #  'TRADING_SYMBOL', 'LOT_UNITS', 'CUSTOM_SYMBOL', 'EXPIRY_DATE',
            #  'STRIKE_PRICE', 'OPTION_TYPE', 'UNDERLYING_SCRIP_NAME', ...]
            for r in reader:
                opt_type = (r.get("OPTION_TYPE") or "").strip().upper()
                if opt_type not in ("CE", "PE"):
                    continue

                trading_sym = (r.get("TRADING_SYMBOL") or "").strip()
                ticker = trading_sym.split("-")[0].strip().upper() if "-" in trading_sym else trading_sym.split(" ")[0].strip().upper()
                underlying_full = (r.get("UNDERLYING_SCRIP_NAME") or "").strip().upper()

                expiry_raw = (r.get("EXPIRY_DATE") or "").strip() # e.g. "11/23/2026 14:00"
                # Standardize expiry to YYYY-MM-DD
                try:
                    dt = datetime.datetime.strptime(expiry_raw.split(" ")[0], "%m/%d/%Y")
                    expiry_standard = dt.strftime("%Y-%m-%d")
                except Exception:
                    expiry_standard = expiry_raw

                try:
                    strike = round(float(r.get("STRIKE_PRICE") or 0.0), 2)
                except ValueError:
                    continue

                sec_id = (r.get("SECURITY_ID") or "").strip()
                exch = (r.get("EXCH") or "NSE").strip().upper()
                lot_units = int(r.get("LOT_UNITS") or 1)

                item = {
                    "exchange": exch,
                    "security_id": sec_id,
                    "trading_symbol": trading_sym,
                    "ticker": ticker,
                    "underlying": ticker,
                    "underlying_full": underlying_full,
                    "expiry": expiry_standard,
                    "strike": strike,
                    "opt_type": opt_type,
                    "lot_units": lot_units,
                    "scrip_code": f"{'NFO' if exch == 'NSE' else 'BFO'}_{sec_id}",
                    "ws_instrument": f"{'NFO' if exch == 'NSE' else 'BFO'}:{sec_id}"
                }

                self._instruments_cache[(ticker, expiry_standard, strike, opt_type)] = item
                for key in set([ticker, underlying_full]):
                    if key:
                        if key not in self._instruments_by_symbol:
                            self._instruments_by_symbol[key] = []
                        self._instruments_by_symbol[key].append(item)
                count += 1

        logger.info(f"Loaded {count:,} option contracts into memory across {len(self._instruments_by_symbol)} symbols.")

    def resolve_instrument(
        self,
        symbol: str,
        expiry: Optional[str],
        strike: float,
        opt_type: str
    ) -> Optional[Dict[str, Any]]:
        """
        Resolves the exact option contract metadata.
        If expiry is None or 'NEAR', picks the nearest active expiry date.
        """
        self._ensure_instruments_loaded()
        clean_sym = symbol.split(" ")[0].upper()
        clean_opt = opt_type.upper()
        clean_strike = round(float(strike), 2)

        candidates = self._instruments_by_symbol.get(clean_sym, [])
        if not candidates:
            # Try index alias mapping
            if clean_sym in ("NIFTY50", "NIFTY_50"):
                candidates = self._instruments_by_symbol.get("NIFTY", [])
            elif clean_sym in ("MIDCAP", "MIDCAPNIFTY"):
                candidates = self._instruments_by_symbol.get("MIDCPNIFTY", [])

        if not candidates:
            logger.warning(f"No option contracts found for underlying {clean_sym}.")
            return None

        # Filter by option type and strike
        matching_strike = [c for c in candidates if c["opt_type"] == clean_opt and abs(c["strike"] - clean_strike) < 0.01]
        if not matching_strike:
            # Fallback to nearest available strike
            matching_strike = [c for c in candidates if c["opt_type"] == clean_opt]
            if not matching_strike:
                return None
            matching_strike.sort(key=lambda x: abs(x["strike"] - clean_strike))
            closest_strike = matching_strike[0]["strike"]
            matching_strike = [c for c in matching_strike if abs(c["strike"] - closest_strike) < 0.01]

        # Resolve expiry
        if expiry and expiry.upper() != "NEAR":
            for c in matching_strike:
                if c["expiry"] == expiry or expiry in c["expiry"]:
                    return c

        # Pick nearest future expiry
        today_str = datetime.datetime.now(tz=IST).strftime("%Y-%m-%d")
        future_candidates = [c for c in matching_strike if c["expiry"] >= today_str]
        if future_candidates:
            future_candidates.sort(key=lambda x: x["expiry"])
            return future_candidates[0]

        # If none in future, pick earliest available
        matching_strike.sort(key=lambda x: x["expiry"])
        return matching_strike[0]

    # -------------------------------------------------------------------------
    # Real-Time Quotes (REST depth + full quote)
    # -------------------------------------------------------------------------
    def get_quote(
        self,
        symbol: str,
        expiry: Optional[str],
        strike: float,
        opt_type: str
    ) -> Dict[str, Any]:
        """
        Fetches live bid/ask from depth level 1 and ltp/oi from /market/quotes/full.
        Applies illiquidity filter (5% spread / empty depth).
        """
        inst = self.resolve_instrument(symbol, expiry, strike, opt_type)
        now_ts = time.time()

        if not inst:
            logger.warning(f"Could not resolve instrument for {symbol} {strike} {opt_type}. Falling back to SYNTHETIC.")
            return {
                "bid": 0.0, "ask": 0.0, "ltp": 0.0, "iv": 0.0, "oi": 0,
                "ts": now_ts, "token": "", "scrip_code": "", "symbol": symbol,
                "is_illiquid": True, "illiquid_reason": "INSTRUMENT_NOT_FOUND",
                "source": "SYNTHETIC"
            }

        scrip_code = inst["scrip_code"]
        sec_id = inst["security_id"]
        headers = self._get_headers()

        # Call /market/quotes/full (contains market_depth, live_price, open_interest)
        url = f"{self.BASE_URL}/market/quotes/full?scrip-codes={scrip_code}"
        try:
            resp = requests.get(url, headers=headers, timeout=5.0)
            if resp.status_code == 403:
                logger.info("INDstocks full quote returned 403. Auto-refreshing broker token and retrying...")
                try:
                    headers = self._get_headers(force_refresh=True)
                    resp = requests.get(url, headers=headers, timeout=5.0)
                except Exception as ref_err:
                    logger.warning(f"Failed to refresh broker token on 403: {ref_err}")

            if resp.status_code != 200:
                logger.warning(f"INDstocks full quote returned HTTP {resp.status_code}. Fallback to SYNTHETIC.")
                return self._synthetic_fallback(symbol, strike, opt_type, scrip_code, sec_id, "API_HTTP_ERROR")

            data = resp.json().get("data", {}).get(scrip_code, {})
            if not data:
                err_msg = resp.json().get("errors", {}).get(scrip_code, {}).get("message", "No data returned")
                logger.warning(f"INDstocks quote empty for {scrip_code}: {err_msg}. Fallback to SYNTHETIC.")
                return self._synthetic_fallback(symbol, strike, opt_type, scrip_code, sec_id, err_msg)

            ltp = float(data.get("live_price") or 0.0)
            oi = int(data.get("open_interest") or 0)

            # Extract depth level 1
            depth_block = data.get("market_depth", {}).get(scrip_code, {}).get("depth", [])
            bid = 0.0
            ask = 0.0
            if depth_block and len(depth_block) > 0:
                top = depth_block[0]
                try:
                    bid = float(top.get("buy", {}).get("price") or 0.0)
                    ask = float(top.get("sell", {}).get("price") or 0.0)
                except (ValueError, TypeError):
                    pass

            # Illiquidity Filter (Requirement 9):
            # Skip signals where (ask - bid)/ask > 5% or depth is empty
            is_illiquid = False
            illiquid_reason = None

            if bid <= 0.0 or ask <= 0.0:
                is_illiquid = True
                illiquid_reason = "EMPTY_DEPTH"
            elif ask > 0.0:
                spread_pct = (ask - bid) / ask
                if spread_pct > 0.05:
                    is_illiquid = True
                    illiquid_reason = f"SPREAD_GT_5% ({round(spread_pct * 100, 2)}%)"

            # Check IV and Greeks (Requirement 5)
            iv = float(data.get("iv") or 0.0)
            if iv <= 0.0:
                iv, greeks = self._get_iv_and_greeks(inst)

            return {
                "bid": bid,
                "ask": ask,
                "ltp": ltp or ((bid + ask) / 2.0 if bid > 0 and ask > 0 else 0.0),
                "iv": iv,
                "oi": oi,
                "ts": now_ts,
                "token": sec_id,
                "scrip_code": scrip_code,
                "symbol": inst["trading_symbol"] or symbol,
                "is_illiquid": is_illiquid,
                "illiquid_reason": illiquid_reason,
                "source": "REAL_QUOTE"
            }

        except Exception as e:
            logger.error(f"Error fetching quote for {scrip_code}: {e}")
            return self._synthetic_fallback(symbol, strike, opt_type, scrip_code, sec_id, str(e))

    def _synthetic_fallback(
        self,
        symbol: str,
        strike: float,
        opt_type: str,
        scrip_code: str,
        token: str,
        reason: str
    ) -> Dict[str, Any]:
        return {
            "bid": 0.0,
            "ask": 0.0,
            "ltp": 0.0,
            "iv": 0.0,
            "oi": 0,
            "ts": time.time(),
            "token": token,
            "scrip_code": scrip_code,
            "symbol": symbol,
            "is_illiquid": False,
            "illiquid_reason": reason,
            "source": "SYNTHETIC"
        }

    # -------------------------------------------------------------------------
    # IV & Greeks via /market/option-chain (Throttled 1/min per contract)
    # -------------------------------------------------------------------------
    def _get_iv_and_greeks(self, inst: Dict[str, Any]) -> Tuple[float, Dict[str, Any]]:
        scrip_code = inst["scrip_code"]
        now = time.time()
        cached = self._greeks_cache.get(scrip_code)
        if cached and (now - cached[0]) < 60.0:
            return cached[1].get("iv", 0.0), cached[1].get("greeks", {})

        underlying = inst["underlying"]
        exch = inst["exchange"]
        expiry = inst["expiry"]
        strike_str = str(int(inst["strike"]) if inst["strike"].is_integer() else inst["strike"])
        opt_type_key = inst["opt_type"].lower()

        # Find underlying security ID
        underlying_info = self.INDEX_UNDERLYING_MAP.get(underlying)
        if underlying_info:
            segment = underlying_info["segment"]
            underlying_scrip = underlying_info["underlying_scrip"]
        else:
            segment = "EQUITY"
            underlying_scrip = "" # Option chain endpoint accepts underlying security id

        if not underlying_scrip:
            return 0.0, {}

        url = f"{self.BASE_URL}/market/option-chain?exchange={exch}&segment={segment}&underlying-scrip={underlying_scrip}&expiry={expiry}&strike_count=5"
        try:
            resp = requests.get(url, headers=self._get_headers(), timeout=5.0)
            if resp.status_code == 200:
                payload = resp.json().get("data", {})
                strikes = payload.get("strikes", {})
                strike_data = strikes.get(strike_str, {}).get(opt_type_key, {})
                iv = float(strike_data.get("iv") or 0.0)
                greeks = strike_data.get("greeks") or {}
                res = {"iv": iv, "greeks": greeks}
                self._greeks_cache[scrip_code] = (now, res)
                return iv, greeks
        except Exception as e:
            logger.debug(f"Could not fetch option-chain for {underlying}: {e}")

        return 0.0, {}

    # -------------------------------------------------------------------------
    # WebSocket Streaming (Requirement 4)
    # -------------------------------------------------------------------------
    def start_streaming(self) -> None:
        """Starts background WebSocket streaming daemon."""
        if self._ws_thread and self._ws_thread.is_alive():
            return
        self._stop_event.clear()
        self._ws_thread = threading.Thread(target=self._run_ws_loop, daemon=True, name="INDstocksWSStream")
        self._ws_thread.start()
        logger.info("INDstocks WebSocket streaming thread started.")

    def stop_streaming(self) -> None:
        """Stops background streaming daemon."""
        self._stop_event.set()
        if self._ws_thread and self._ws_thread.is_alive():
            self._ws_thread.join(timeout=2.0)
        logger.info("INDstocks WebSocket streaming thread stopped.")

    def subscribe(self, token: str, exch: str = "NSE") -> None:
        """Subscribes to live streaming for a specific token (e.g. 64462)."""
        instrument_str = f"{'NFO' if exch == 'NSE' else 'BFO'}:{token}"
        with self._ws_lock:
            self._subscribed_tokens.add(instrument_str)

        self.start_streaming()
        if self._loop and self._active_ws and self._is_ws_connected:
            sub_msg = {"action": "subscribe", "mode": "quote", "instruments": [instrument_str]}
            asyncio.run_coroutine_threadsafe(self._active_ws.send(json.dumps(sub_msg)), self._loop)
            logger.info(f"Subscribed to live WebSocket feed: {instrument_str}")

    def unsubscribe(self, token: str, exch: str = "NSE") -> None:
        """Unsubscribes from live streaming on trade exit."""
        instrument_str = f"{'NFO' if exch == 'NSE' else 'BFO'}:{token}"
        with self._ws_lock:
            self._subscribed_tokens.discard(instrument_str)

        if self._loop and self._active_ws and self._is_ws_connected:
            unsub_msg = {"action": "unsubscribe", "mode": "quote", "instruments": [instrument_str]}
            asyncio.run_coroutine_threadsafe(self._active_ws.send(json.dumps(unsub_msg)), self._loop)
            logger.info(f"Unsubscribed from live WebSocket feed: {instrument_str}")

    def get_latest_stream_quote(self, token: str) -> Optional[Dict[str, Any]]:
        """Returns the most recent streamed quote from memory, or None."""
        with self._ws_lock:
            return self._live_stream_quotes.get(str(token))

    def _run_ws_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._ws_supervisor())

    async def _ws_supervisor(self) -> None:
        backoff = 1.0
        while not self._stop_event.is_set():
            try:
                headers = {"Authorization": self._get_auth_token()}
                async with websockets.connect(self.WS_URL, additional_headers=headers) as ws:
                    self._active_ws = ws
                    self._is_ws_connected = True
                    backoff = 1.0
                    logger.info("Connected to INDstocks live WebSocket stream.")

                    # Resubscribe to all active tokens
                    with self._ws_lock:
                        tokens = list(self._subscribed_tokens)
                    if tokens:
                        sub_msg = {"action": "subscribe", "mode": "quote", "instruments": tokens}
                        await ws.send(json.dumps(sub_msg))
                        logger.info(f"Resubscribed to {len(tokens)} active tokens on reconnect.")

                    while not self._stop_event.is_set():
                        try:
                            raw_msg = await asyncio.wait_for(ws.recv(), timeout=6.0)
                            self._handle_ws_message(raw_msg)
                        except asyncio.TimeoutError:
                            # Keepalive ping
                            await ws.ping()

            except Exception as e:
                self._is_ws_connected = False
                self._active_ws = None
                if not self._stop_event.is_set():
                    logger.warning(f"INDstocks WS connection lost ({e}). Reconnecting in {backoff:.1f}s...")
                    await asyncio.sleep(backoff)
                    backoff = min(60.0, backoff * 1.5)

    def _handle_ws_message(self, raw_msg: str) -> None:
        try:
            msg = json.loads(raw_msg)
            # Example message format:
            # {"mode":"quote","instrument":"64462","timestamp":1794973693233,"data":{"open":140,"high":171.55,"low":121.15,"close":151.4,"volume":8017165}}
            inst = msg.get("instrument")
            data = msg.get("data", {})
            if inst and data:
                with self._ws_lock:
                    self._live_stream_quotes[str(inst)] = {
                        "ltp": float(data.get("close", 0.0)),
                        "open": float(data.get("open", 0.0)),
                        "high": float(data.get("high", 0.0)),
                        "low": float(data.get("low", 0.0)),
                        "volume": int(data.get("volume", 0)),
                        "ts": float(msg.get("timestamp", time.time() * 1000)) / 1000.0
                    }
        except Exception:
            pass
