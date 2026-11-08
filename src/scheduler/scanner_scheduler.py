"""
FinPlus+ Scanner Scheduler & Continuous Evaluation Loop
Orchestrates data fetching, closed-candle LuxAlgo range calculation,
fail-closed App #1 confluence check, and tiered NTFY alert dispatching.
"""

import time
import asyncio
from typing import Dict, Any, List
from src.feeds.market_data_feed import MarketDataFeed
from src.analytics.range_detector_engine import RangeDetectorEngine
from src.analytics.regime_interceptor import App1RegimeInterceptor
from src.notifications.ntfy_dispatcher import NTFYDispatcher
from src.utils.time_and_logger import setup_logger, get_utc_iso, get_ist_time_str, is_market_open_ist

logger = setup_logger("ScannerScheduler")

class ScannerScheduler:
    def __init__(self):
        self.feed = MarketDataFeed(cache_ttl_seconds=60, max_lot_size=500, top_n_stocks=20)
        self.detector = RangeDetectorEngine(length=20, mult=1.0, atr_len=500)
        self.interceptor = App1RegimeInterceptor(timeout_secs=2.0)
        self.dispatcher = NTFYDispatcher()
        
        self.latest_scan_results: List[Dict[str, Any]] = []
        self.last_scan_utc: str = get_utc_iso()
        self.is_running: bool = False

    def scan_universe_now(self) -> List[Dict[str, Any]]:
        """Scans all dynamic universe items synchronously (runs in worker pool)."""
        universe = self.feed.get_all_universe_items()
        results = []
        
        logger.info(f"Starting universe scan ({len(universe)} dynamic instruments)...")
        
        for item in universe:
            ticker = item.get("yf_ticker")
            sym = item.get("symbol")
            try:
                df = self.feed.fetch_15m_candles(ticker, period="59d")
                if df.empty:
                    continue
                    
                eval_res = self.detector.evaluate_instrument(df, item)
                
                # Cross-reference with App #1 (Fail-Closed 2s Timeout)
                sig_dir = eval_res.get("signal_direction", 0)
                conf_status, conf_badge, regime_data = self.interceptor.evaluate_confluence(
                    sig_dir, item.get("category", "STOCK")
                )
                
                eval_res["regime_confluence"] = conf_status
                eval_res["regime_badge"] = conf_badge
                results.append(eval_res)
                
                # Alert Dispatched ONLY IF NOT VETOED and Score >= 50
                score = eval_res.get("confluence_score", 0)
                state = eval_res.get("ignition_state")
                is_vetoed = "VETOED" in conf_status
                
                if score >= 50 and not is_vetoed:
                    if state in ["BULLISH_IGNITION", "BEARISH_IGNITION", "RANGE_BREAKOUT_LONG", "RANGE_BREAKDOWN_SHORT"]:
                        self.dispatcher.send_early_ignition_alert(eval_res, conf_status, conf_badge)
                        
                elif state == "COILING_ALERT":
                    self.dispatcher.send_coiling_alert(eval_res)
                    
            except Exception as e:
                logger.error(f"Error evaluating {sym} ({ticker}): {e}")
                
        self.latest_scan_results = results
        self.last_scan_utc = get_utc_iso()
        logger.info(f"Completed universe scan ({len(results)} items evaluated).")
        return results

    async def start_loop(self):
        """Asynchronous scheduler background task."""
        self.is_running = True
        logger.info("ScannerScheduler background daemon started.")
        
        await asyncio.to_thread(self.scan_universe_now)
        
        while self.is_running:
            try:
                sleep_secs = 60 if is_market_open_ist() else 180
                await asyncio.sleep(sleep_secs)
                await asyncio.to_thread(self.scan_universe_now)
            except asyncio.CancelledError:
                logger.info("ScannerScheduler task cancelled.")
                break
            except Exception as e:
                logger.error(f"Unexpected error in scheduler loop: {e}")
                await asyncio.sleep(30)

    def stop_loop(self):
        self.is_running = False
