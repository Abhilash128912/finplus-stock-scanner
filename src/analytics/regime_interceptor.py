"""
FinPlus+ App #1 Air Traffic Control Interceptor
MANDATE COMPLIANCE:
1. Fail-Closed Security: Must timeout in 2.0s. If App #1 is down or slow, the alert is strictly tagged VETOED, never CONFIRMED.
2. Unified Confluence Rule: Uses App #1's single Composite Trend Index (CTI) score (-100 to +100) which already integrates
   benchmark breadth (WABI), all 2,266 NSE equities A/D ratio, and India VIX / Derivatives.
"""

import time
import urllib.request
import urllib.error
import socket
import json
from typing import Dict, Any, Tuple
from src.utils.time_and_logger import setup_logger, get_utc_iso

logger = setup_logger("RegimeInterceptor")

APP1_LOCAL_URL = "http://127.0.0.1:8000/api/regime"
APP1_CLOUD_URL = "https://finplus-index-breadth.onrender.com/api/regime"

class App1RegimeInterceptor:
    def __init__(self, local_url: str = APP1_LOCAL_URL, fallback_url: str = APP1_CLOUD_URL, timeout_secs: float = 2.0, cache_ttl_seconds: float = 15.0):
        self.local_url = local_url
        self.fallback_url = fallback_url
        self.timeout_secs = timeout_secs
        self.cache_ttl = cache_ttl_seconds
        self._cached_result: Tuple[bool, Dict[str, Any]] = None
        self._cache_ts: float = 0.0

    def fetch_current_regime(self) -> Tuple[bool, Dict[str, Any]]:
        """
        Fetches live regime from App #1 with strict 2.0-second timeout.
        Caches result for 15s to keep scan loops sub-second.
        Returns (is_success, regime_payload).
        """
        now = time.time()
        if self._cached_result is not None and (now - self._cache_ts) < self.cache_ttl:
            return self._cached_result

        for url in [self.local_url, self.fallback_url]:
            try:
                req = urllib.request.Request(url, headers={'User-Agent': 'FinPlus-StockScanner/2.0'})
                with urllib.request.urlopen(req, timeout=self.timeout_secs) as resp:
                    if resp.status == 200:
                        data = json.loads(resp.read().decode('utf-8'))
                        self._cached_result = (True, data)
                        self._cache_ts = now
                        return self._cached_result
            except (urllib.error.URLError, socket.timeout, TimeoutError, Exception) as e:
                logger.warning(f"App #1 probe to {url} failed or timed out (>2.0s): {e}")
                continue
                
        # FAIL CLOSED: Never assume confirmed when unreachable
        fail_closed_payload = {
            "trend": {
                "code": "GATE_TIMEOUT",
                "label": "AIR TRAFFIC CONTROL OFFLINE (FAIL-CLOSED)",
                "score": 0,
                "is_divergence": True,
                "description": "App #1 did not respond within 2.0s. All breakout entries are strictly VETOED for capital protection."
            }
        }
        self._cached_result = (False, fail_closed_payload)
        self._cache_ts = now
        return self._cached_result

    def evaluate_confluence(self, signal_direction: int, instrument_type: str = "STOCK") -> Tuple[str, str, Dict[str, Any]]:
        """
        Cross-verifies an ignition signal against App #1 Macro Regime.
        FAIL-CLOSED: If App #1 is down or slow, the alert is tagged VETOED, never CONFIRMED.
        """
        if signal_direction == 0:
            return "NO_SIGNAL", "STANDBY", {}
            
        success, regime = self.fetch_current_regime()
        
        # FAIL CLOSED GUARD
        if not success:
            logger.warning("Veto Gate triggered FAIL-CLOSED: App #1 unreachable or slow.")
            return "VETOED_GATE_TIMEOUT", "VETOED (AIR TRAFFIC UNREACHABLE)", regime

        trend_score = regime.get("trend", {}).get("score", 0)
        is_divergent = regime.get("trend", {}).get("is_divergence", False)
        trend_code = regime.get("trend", {}).get("code", "")
        heavyweights = regime.get("metrics", {}).get("heavyweights_above", "0/5")

        # UNIFIED CONFLUENCE METRIC: Composite Trend Index (CTI)
        # Pillar 1 (WABI) + Pillar 2 (All-NSE A/D) + Pillar 3 (VIX & Derivatives)
        
        # 1. Bullish Signal (Call Option / Long Breakout)
        if signal_direction == 1:
            if trend_score >= 30 and not is_divergent:
                return "APPROVED_HIGH_CONFLUENCE", "GREEN LIT (CTI UPTREND ALIGNED)", regime
            elif is_divergent or "CHOPPY" in trend_code:
                return "VETOED_CHOP_RISK", "STANDBY (MARKET CHOPPY / DIVERGENCE)", regime
            elif trend_score <= -25 or "DOWNTREND" in trend_code:
                return "VETOED_COUNTER_TREND", f"VETOED (MACRO DOWNTREND • CTI {trend_score}/100)", regime
            else:
                return "VETOED_LOW_MOMENTUM", f"STANDBY (MARKET CHOPPY • CTI +{trend_score} BELOW +30 THRESHOLD)", regime
                
        # 2. Bearish Signal (Put Option / Short Breakdown)
        elif signal_direction == -1:
            if trend_score <= -30:
                return "APPROVED_HIGH_CONFLUENCE", "GREEN LIT (CTI DOWNTREND ALIGNED)", regime
            elif trend_score >= 30:
                return "VETOED_COUNTER_TREND", f"VETOED (MACRO UPTREND • CTI +{trend_score}/100)", regime
            elif is_divergent:
                # In broad market divergence, shorting overvalued scrips has edge
                return "APPROVED_HIGH_CONFLUENCE", "GREEN LIT (DIVERGENCE SHORT)", regime
            else:
                return "VETOED_LOW_MOMENTUM", f"STANDBY (MARKET CHOPPY • CTI {trend_score} INSUFFICIENT)", regime
                
        return "VETOED_DEFAULT", "VETOED (FAIL-CLOSED)", regime
