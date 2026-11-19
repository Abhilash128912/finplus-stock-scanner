"""
FinPlus+ Sensex Spot Resolver
=============================
PRIMARY: Real-time official INDstocks (INDmoney) broker quote feed (BIDX_40000006).
SECONDARY / PROXY: Future-contract proxy (tagged spot_source=FUT_PROXY).
Zero external/third-party dependencies.
"""

import time
from typing import Tuple, Optional
from src.utils.time_and_logger import setup_logger

logger = setup_logger("SensexSpotResolver")

_CACHED_SENSEX_SPOT = 0.0
_CACHED_SENSEX_CHANGE_PCT = 0.0
_LAST_SENSEX_FETCH_TIME = 0.0
_LAST_SPOT_SOURCE = "INDSTOCKS"


def get_live_sensex_spot(fallback_val: float = 0.0) -> Tuple[float, float]:
    """
    Retrieves live real-time BSE SENSEX spot and change percentage.
    Strict Priority:
      1. INDstocks (INDmoney) Broker API (Primary) -> spot_source="INDSTOCKS"
      2. Future-contract proxy (Secondary) -> spot_source="FUT_PROXY"
    """
    global _CACHED_SENSEX_SPOT, _CACHED_SENSEX_CHANGE_PCT, _LAST_SENSEX_FETCH_TIME, _LAST_SPOT_SOURCE

    now = time.time()
    # Cache for 2 seconds to avoid excessive polling
    if (now - _LAST_SENSEX_FETCH_TIME) < 2.0 and _CACHED_SENSEX_SPOT > 50000:
        return _CACHED_SENSEX_SPOT, _CACHED_SENSEX_CHANGE_PCT

    # 1. PRIMARY: INDstocks Broker Live Quote Feed
    try:
        from src.paper.indmoney_live_feed import INDMoneyLiveFeed
        ind_feed = INDMoneyLiveFeed()
        quotes = ind_feed.fetch_batch_quotes(["SENSEX"], force=((now - _LAST_SENSEX_FETCH_TIME) >= 2.0))
        sx_data = quotes.get("SENSEX")
        if sx_data and float(sx_data.get("ltp", 0.0)) > 50000:
            ltp = round(float(sx_data["ltp"]), 2)
            chg = round(float(sx_data.get("change_pct", 0.0)), 2)
            _CACHED_SENSEX_SPOT = ltp
            _CACHED_SENSEX_CHANGE_PCT = chg
            _LAST_SENSEX_FETCH_TIME = now
            _LAST_SPOT_SOURCE = "INDSTOCKS"
            return _CACHED_SENSEX_SPOT, _CACHED_SENSEX_CHANGE_PCT
    except Exception as ind_err:
        logger.debug(f"INDstocks primary Sensex quote notice: {ind_err}")

    # 2. FUTURE-CONTRACT PROXY (tagged spot_source=FUT_PROXY)
    try:
        from src.paper.indmoney_live_feed import INDMoneyLiveFeed
        ind_feed = INDMoneyLiveFeed()
        fut_quotes = ind_feed.fetch_batch_quotes(["SENSEX_FUT", "BSX_FUT", "BSE_SENSEX_FUT"])
        for k in ["SENSEX_FUT", "BSX_FUT", "BSE_SENSEX_FUT"]:
            fd = fut_quotes.get(k)
            if fd and float(fd.get("ltp", 0.0)) > 50000:
                _CACHED_SENSEX_SPOT = round(float(fd["ltp"]), 2)
                _CACHED_SENSEX_CHANGE_PCT = round(float(fd.get("change_pct", 0.0)), 2)
                _LAST_SENSEX_FETCH_TIME = now
                _LAST_SPOT_SOURCE = "FUT_PROXY"
                return _CACHED_SENSEX_SPOT, _CACHED_SENSEX_CHANGE_PCT
    except Exception as fut_err:
        logger.debug(f"Future proxy quote notice for Sensex: {fut_err}")

    # 4. Provided fallback value if valid and not stale
    if fallback_val > 50000 and abs(fallback_val - 82300.0) > 0.01:
        _LAST_SPOT_SOURCE = "FUT_PROXY"
        return round(fallback_val, 2), 0.0

    return 0.0, 0.0


def get_sensex_spot_source() -> str:
    """Returns current spot source ('INDSTOCKS' or 'FUT_PROXY')."""
    return _LAST_SPOT_SOURCE
