"""
FinPlus+ Scanner Scheduler & Continuous Evaluation Loop
Orchestrates data fetching, closed-candle LuxAlgo range calculation,
fail-closed App #1 confluence check, Shortlist Engine evaluation,
and strict 30-minute NTFY pulse dispatching.
"""

import time
import asyncio
from typing import Dict, Any, List
from src.feeds.market_data_feed import MarketDataFeed
from src.analytics.range_detector_engine import RangeDetectorEngine
from src.analytics.regime_interceptor import App1RegimeInterceptor
from src.analytics.shortlist_engine import ShortlistEngine
from src.notifications.ntfy_dispatcher import NTFYDispatcher
from src.paper.live_paper_tracker import LivePaperTracker
from src.paper.indmoney_live_feed import INDMoneyLiveFeed
from src.utils.time_and_logger import setup_logger, get_utc_iso, get_ist_time_str, is_market_open_ist
from src.utils.indian_market_calendar import is_equity_market_open_ist, is_nse_holiday

logger = setup_logger("ScannerScheduler")

class ScannerScheduler:
    def __init__(self):
        self.feed = MarketDataFeed(
            cache_ttl_seconds=60,
            max_lot_size=500,
            top_n_fno_stocks=150,
            top_n_cash_stocks=750
        )
        self.detector = RangeDetectorEngine(length=20, mult=1.0, atr_len=500)
        self.interceptor = App1RegimeInterceptor(timeout_secs=2.0)
        self.shortlist_engine = ShortlistEngine()
        self.dispatcher = NTFYDispatcher(min_interval_seconds=1800) # Strict 30 mins
        self.paper_tracker = LivePaperTracker()
        self.indmoney_feed = INDMoneyLiveFeed(cache_ttl_seconds=5.0)
        
        self.latest_scan_results: List[Dict[str, Any]] = []
        self.latest_shortlist: Dict[str, Any] = {}
        self.last_scan_utc: str = get_utc_iso()
        self.is_running: bool = False
        self._dispatched_slots: set = set()

    def scan_universe_now(self) -> List[Dict[str, Any]]:
        """Scans all dynamic universe items synchronously (runs in worker pool)."""
        is_open, open_reason = is_equity_market_open_ist()
        if not is_open:
            logger.info(f"Market Closed Gate Active: {open_reason}. Scanning skipped.")
            return self.latest_scan_results

        universe = self.feed.get_all_universe_items()
        results = []
        
        logger.info(f"Starting universe scan ({len(universe)} dynamic instruments: F&O Options + Intraday Cash)...")
        
        # Batch prefetch candles in chunks of 35 (cuts 7-min scan down to ~25s and eliminates rate limits)
        all_tickers = [item.get("yf_ticker") for item in universe if item.get("yf_ticker")]
        try:
            self.feed.prefetch_batch_15m_candles(all_tickers, period="5d")
        except Exception as e:
            logger.warning(f"Batch prefetch exception: {e}")

        for item in universe:
            ticker = item.get("yf_ticker")
            sym = item.get("symbol")
            try:
                df = self.feed.fetch_15m_candles(ticker, period="5d")
                if df.empty:
                    continue
                    
                eval_res = self.detector.evaluate_instrument(df, item)
                
                # Fetch instantaneous live tick from INDmoney broker API (or fast_info fallback)
                try:
                    ind_quote = self.indmoney_feed.get_single_quote(sym) if self.indmoney_feed else None
                    if ind_quote and ind_quote > 0:
                        live_p = round(float(ind_quote), 2)
                        eval_res["live_ltp"] = live_p
                        eval_res["candle_close"] = eval_res["last_price"]
                        eval_res["last_price"] = live_p
                        eval_res["price_source"] = "INDstocks Broker API"
                        if eval_res.get("range_top"):
                            r_top = eval_res["range_top"]
                            move_from_top_pct = round(((live_p - r_top) / max(1.0, r_top)) * 100.0, 2)
                            eval_res["move_from_breakout_pct"] = move_from_top_pct
                    else:
                        live_ticker = self.feed._get_ticker(ticker) if hasattr(self.feed, '_get_ticker') else None
                        if live_ticker and hasattr(live_ticker, 'fast_info'):
                            fast_p = getattr(live_ticker.fast_info, 'last_price', None)
                            if fast_p and fast_p > 0:
                                live_p = round(float(fast_p), 2)
                                eval_res["live_ltp"] = live_p
                                eval_res["candle_close"] = eval_res["last_price"]
                                eval_res["last_price"] = live_p
                                eval_res["price_source"] = "Exchange Feed"
                                if eval_res.get("range_top"):
                                    r_top = eval_res["range_top"]
                                    move_from_top_pct = round(((live_p - r_top) / max(1.0, r_top)) * 100.0, 2)
                                    eval_res["move_from_breakout_pct"] = move_from_top_pct
                except Exception:
                    pass
                
                # Cross-reference with App #1 (Fail-Closed 2s Timeout)
                sig_dir = eval_res.get("signal_direction", 0)
                conf_status, conf_badge, regime_data = self.interceptor.evaluate_confluence(
                    sig_dir, item.get("category", "STOCK"), eval_res.get("rvol", 1.0), eval_res.get("confluence_score", 0)
                )
                
                eval_res["regime_confluence"] = conf_status
                eval_res["regime_badge"] = conf_badge
                eval_res["regime_data"] = regime_data
                results.append(eval_res)

                # Progressive update: Keep dashboard and shortlist fresh every 10 instruments
                if len(results) % 10 == 0:
                    current_sorted = sorted(results, key=lambda x: (x.get("confluence_score", 0), abs(x.get("pct_change_pdc", 0.0))), reverse=True)
                    self.latest_scan_results = current_sorted
                    self.latest_shortlist = self.shortlist_engine.select_best_candidates(current_sorted)
                    try:
                        self.paper_tracker.ingest_shortlist(self.latest_shortlist)
                    except Exception:
                        pass
                
            except Exception as e:
                logger.error(f"Error evaluating {sym} ({ticker}): {e}")
                
        # Default Sort: Score & Breakdown (Highest Confluence Score First)
        results.sort(key=lambda x: (x.get("confluence_score", 0), abs(x.get("pct_change_pdc", 0.0))), reverse=True)
        self.latest_scan_results = results
        self.last_scan_utc = get_utc_iso()
        
        # Evaluate 30-Minute Best-in-Class Shortlist
        shortlist = self.shortlist_engine.select_best_candidates(results)
        self.latest_shortlist = shortlist
        
        # Auto-ingest candidates into 24/7 Live Paper Tracker
        try:
            self.paper_tracker.ingest_shortlist(shortlist)
        except Exception as e:
            logger.error(f"Error auto-ingesting shortlist to paper tracker: {e}")

        # Dispatch 30m pulse (enforcing 30m cooldown)
        self.dispatcher.send_30m_shortlist_pulse(shortlist)
        
        logger.info(f"Completed universe scan ({len(results)} items evaluated). Shortlist updated.")
        return results

    async def _clock_dispatcher_loop(self):
        """
        Dedicated 5-second precision wall-clock dispatcher.
        Strictly enforces user-mandated NSE/BSE schedule on weekdays:
        1. 09:00 AM IST: Today's Agenda (Tactical Morning Briefing)
        2. 09:30 AM IST: First Status & Explicit Trade Suggestions
        3. Every 30 mins (10:00, 10:30, 11:00, 11:30, 12:00, 12:30, 13:00, 13:30, 14:00, 14:30 IST): 30m Pulse
        4. 03:00 PM (15:00) IST: End of Trade Summary
        """
        from datetime import datetime, timezone, timedelta
        logger.info("Clock Dispatcher Loop active (5-second precision tick).")

        while self.is_running:
            try:
                ist_now = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
                # Weekdays only (Monday = 0 to Friday = 4)
                if ist_now.weekday() < 5:
                    date_str = ist_now.strftime("%Y-%m-%d")
                    hh = ist_now.hour
                    mm = ist_now.minute

                    # Check for official NSE Holiday
                    is_hol, hol_name = is_nse_holiday(ist_now.date())
                    if is_hol:
                        slot_hol = f"{date_str}_holiday_alert"
                        if hh >= 9 and slot_hol not in self._dispatched_slots:
                            self._dispatched_slots.add(slot_hol)
                            logger.info(f"NSE Holiday Gate: Today is {hol_name}. Dispatching holiday notice...")
                            # Send single informational alert that exchange is closed
                            self.dispatcher.send_holiday_notice(hol_name)
                        await asyncio.sleep(60)
                        continue

                    # 1. 09:00 AM IST: Today's Agenda
                    slot_0900 = f"{date_str}_09:00"
                    if hh == 9 and 0 <= mm < 5 and slot_0900 not in self._dispatched_slots:
                        self._dispatched_slots.add(slot_0900)
                        logger.info("Triggering 09:00 AM IST Today's Agenda Briefing...")
                        self.dispatcher.send_morning_tactical_briefing(shortlist=self.latest_shortlist, force=True)

                    # 2. Every 30 Minutes from 09:30 AM to 02:30 PM:
                    # 09:30 (First Status with Suggestions), 10:00, 10:30, 11:00, 11:30, 12:00, 12:30, 13:00, 13:30, 14:00, 14:30
                    schedule_slots = [
                        (9, 30), (10, 0), (10, 30), (11, 0), (11, 30),
                        (12, 0), (12, 30), (13, 0), (13, 30), (14, 0), (14, 30)
                    ]
                    for target_h, target_m in schedule_slots:
                        slot_id = f"{date_str}_{target_h:02d}:{target_m:02d}"
                        if hh == target_h and target_m <= mm < target_m + 5 and slot_id not in self._dispatched_slots:
                            self._dispatched_slots.add(slot_id)
                            label = "09:30 AM First Status & Suggestions" if (target_h, target_m) == (9, 30) else f"{target_h:02d}:{target_m:02d} IST 30m Pulse"
                            logger.info(f"Triggering {label}...")
                            self.dispatcher.send_30m_shortlist_pulse(self.latest_shortlist, force=True)

                    # 3. 03:00 PM (15:00) IST: End of Trade Summary
                    slot_1500 = f"{date_str}_15:00"
                    if hh == 15 and 0 <= mm < 5 and slot_1500 not in self._dispatched_slots:
                        self._dispatched_slots.add(slot_1500)
                        logger.info("Triggering 03:00 PM IST End of Trade Session Summary...")
                        self.dispatcher.send_end_of_trade_summary(shortlist=self.latest_shortlist, force=True)

                await asyncio.sleep(5)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in Clock Dispatcher loop: {e}")
                await asyncio.sleep(10)

    async def _scanner_worker_loop(self):
        """Background 516-universe data refresher."""
        logger.info("Scanner Data Worker Loop active.")
        while self.is_running:
            try:
                sleep_secs = 60 if is_market_open_ist() else 180
                await asyncio.sleep(sleep_secs)
                await asyncio.to_thread(self.scan_universe_now)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in Scanner Worker loop: {e}")
                await asyncio.sleep(30)

    async def start_loop(self):
        """Launches clock dispatcher, background scanner, and live paper tracking loop concurrently."""
        self.is_running = True
        logger.info("ScannerScheduler background daemon starting...")
        
        # Initial scan boot
        await asyncio.to_thread(self.scan_universe_now)
        
        # Run loops concurrently without blocking each other
        await asyncio.gather(
            self._clock_dispatcher_loop(),
            self._scanner_worker_loop(),
            self.paper_tracker.start_polling_loop()
        )

    def stop_loop(self):
        self.is_running = False
        self.paper_tracker.stop_polling_loop()
