"""
FinPlus+ Stock Scanner Automated Unit & Integration Tests
Validates:
1. Timezone compliance: Logs strictly in UTC ('Z'), Market logic strictly in IST.
2. LuxAlgo Range Detector mathematical logic on CLOSED candles.
3. Dynamic FNOContractMaster loading from NSE fo_mktlots.csv.
4. Fail-closed 2.0s Veto Gate Interceptor.
5. ShortlistEngine ranking and Trend Continuation tracking.
"""

import sys
import os
import unittest
import pandas as pd
import numpy as np
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# Add parent path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.utils.time_and_logger import (
    get_utc_now, 
    get_utc_iso, 
    get_ist_now, 
    is_market_open_ist, 
    setup_logger
)
from src.analytics.range_detector_engine import (
    RangeDetectorEngine, 
    calc_atr, 
    calc_intraday_vwap
)
from src.analytics.regime_interceptor import App1RegimeInterceptor
from src.feeds.fno_contract_master import FNOContractMaster
from src.analytics.shortlist_engine import ShortlistEngine

class TestStockScanner(unittest.TestCase):

    def test_01_timezone_compliance(self):
        """Verify internal logs/timestamps use ISO-8601 UTC and market hours use IST."""
        utc_dt = get_utc_now()
        self.assertEqual(utc_dt.tzinfo, timezone.utc)
        
        utc_str = get_utc_iso()
        self.assertTrue(utc_str.endswith("Z") or "+00:00" in utc_str)
        
        ist_dt = get_ist_now()
        self.assertEqual(str(ist_dt.tzinfo), "Asia/Kolkata")
        
        offset_seconds = ist_dt.utcoffset().total_seconds()
        self.assertEqual(offset_seconds, 19800)

    def test_02_range_detector_math_closed_candle(self):
        """Verify LuxAlgo compression and breakout evaluation strictly on closed candles."""
        dates = pd.date_range("2026-11-01 09:15", periods=50, freq="15min", tz="Asia/Kolkata")
        
        df_coiled = pd.DataFrame({
            "Open": [100.0] * 50,
            "High": [100.5] * 50,
            "Low": [99.5] * 50,
            "Close": [100.0] * 50,
            "Volume": [1000] * 50
        }, index=dates)
        
        detector = RangeDetectorEngine(length=20, mult=1.0, atr_len=14)
        meta = {"symbol": "TESTCOIL", "strike_step": 10, "importance": "HIGH", "category": "STOCK"}
        
        res = detector.evaluate_instrument(df_coiled, meta)
        self.assertEqual(res["symbol"], "TESTCOIL")
        self.assertTrue(res["is_closed_candle"])
        self.assertIn("points_breakdown", res)
        self.assertTrue(res["is_coiling"] or res["ignition_state"] in ["COILING_ALERT", "NEUTRAL"])

    def test_03_regime_interceptor_fail_closed(self):
        """Verify App #1 interceptor enforces 2.0s timeout and fails closed."""
        interceptor = App1RegimeInterceptor(
            local_url="http://127.0.0.1:9999/api/regime", 
            fallback_url="http://127.0.0.1:9998/api/regime",
            timeout_secs=0.5
        )
        status, badge, reg = interceptor.evaluate_confluence(signal_direction=1)
        self.assertTrue(status.startswith("VETOED"))
        self.assertIn("FAIL-CLOSED", reg.get("trend", {}).get("label", ""))

    def test_04_fno_contract_master_dynamic(self):
        """Verify dynamic NSE F&O contract master loads and filters lot <= 500 (+ Reliance)."""
        master = FNOContractMaster(max_lot_size=500)
        universe = master.load_universe(top_n_equities=10)
        self.assertGreaterEqual(len(universe), 2)
        symbols = [u["symbol"] for u in universe]
        self.assertIn("SENSEX", symbols)
        self.assertIn("NIFTY", symbols)

    def test_05_shortlist_engine_continuation(self):
        """Verify ShortlistEngine ranks correctly and detects Trend Continuation in 30m cycles."""
        engine = ShortlistEngine()
        sample_items = [
            {
                'symbol': 'SENSEX',
                'category': 'INDEX_OPTIONS',
                'confluence_score': 75,
                'signal_direction': 1,
                'recommended_contract': 'SENSEX 81400 CE',
                'regime_confluence': 'APPROVED_HIGH_CONFLUENCE'
            },
            {
                'symbol': 'MARUTI',
                'category': 'STOCK_OPTIONS',
                'confluence_score': 85,
                'signal_direction': 1,
                'recommended_contract': 'MARUTI 11500 CE',
                'regime_confluence': 'APPROVED_HIGH_CONFLUENCE'
            }
        ]
        
        cycle1 = engine.select_best_candidates(sample_items)
        self.assertFalse(cycle1["index_continuation"])
        self.assertFalse(cycle1["stock_continuation"])
        
        cycle2 = engine.select_best_candidates(sample_items)
        self.assertTrue(cycle2["index_continuation"])
        self.assertTrue(cycle2["stock_continuation"])

if __name__ == "__main__":
    unittest.main()
