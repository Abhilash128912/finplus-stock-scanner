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
from src.storage.schedule_ledger import ScheduleLedger
from src.storage.candle_store import CandleStore
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
        self.ledger = ScheduleLedger()
        self.candle_store = CandleStore()
        
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
        
        # Batch prefetch candles in chunks of 35 (cuts scan down to ~25s and eliminates rate limits)
        all_tickers = [item.get("yf_ticker") for item in universe if item.get("yf_ticker")]
        try:
            self.feed.prefetch_batch_15m_candles(all_tickers, period="5d")
        except Exception as e:
            logger.warning(f"Batch prefetch exception: {e}")

        # Batch prefetch ALL live quotes from INDmoney broker API upfront in one fast sweep
        all_symbols = [item.get("symbol") for item in universe if item.get("symbol")]
        ind_quotes_map = {}
        if self.indmoney_feed:
            try:
                ind_quotes_map = self.indmoney_feed.fetch_batch_quotes(all_symbols)
            except Exception as e:
                logger.warning(f"INDmoney batch prefetch error: {e}")

        for item in universe:
            ticker = item.get("yf_ticker")
            sym = item.get("symbol")
            try:
                df = self.feed.fetch_15m_candles(ticker, period="5d")
                if df.empty:
                    continue
                    
                eval_res = self.detector.evaluate_instrument(df, item)
                
                # Fetch instantaneous live tick from INDmoney broker API cache (or fast_info fallback)
                try:
                    cached_quote = ind_quotes_map.get(sym)
                    ind_quote = cached_quote.get("ltp") if cached_quote else None
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
        Institutional State-Driven Dispatcher.
        Queries the persistent SQLite ScheduleLedger every 5 seconds.
        Ensures idempotent, guaranteed execution of all daily milestones:
        1. 09:00 AM IST: Today's Agenda (Tactical Morning Briefing)
        2. 09:30 AM IST: First Status & Explicit Trade Suggestions
        3. Every 30 mins (10:00, 10:30, 11:00, 11:30, 12:00, 12:30, 13:00, 13:30, 14:00, 14:30 IST): 30m Pulse
        4. 03:00 PM (15:00) IST: End of Trade Summary
        """
        from datetime import datetime, timezone, timedelta
        logger.info("Ledger-Driven Clock Dispatcher Loop active (5-second precision tick).")

        while self.is_running:
            try:
                ist_now = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
                # Weekdays only (Monday = 0 to Friday = 4)
                if ist_now.weekday() < 5:
                    date_str = ist_now.strftime("%Y-%m-%d")
                    ist_now_str = ist_now.strftime("%I:%M %p IST")

                    # Check for official NSE Holiday
                    is_hol, hol_name = is_nse_holiday(ist_now.date())
                    if is_hol:
                        slot_hol = f"{date_str}_holiday_alert"
                        if ist_now.hour >= 9 and slot_hol not in self._dispatched_slots:
                            self._dispatched_slots.add(slot_hol)
                            logger.info(f"NSE Holiday Gate: Today is {hol_name}. Dispatching holiday notice...")
                            self.dispatcher.send_holiday_notice(hol_name)
                        await asyncio.sleep(60)
                        continue

                    # Query the persistent ledger for all due milestones that haven't been completed
                    due_events = self.ledger.get_pending_due_events(ist_now)
                    for ev in due_events:
                        slot_id = ev["slot_id"]
                        
                        # Atomically claim slot in persistent ledger (prevents duplicate execution)
                        if not self.ledger.mark_in_flight(date_str, slot_id):
                            continue

                        logger.info(f"Ledger Trigger: Processing due milestone {slot_id} at {ist_now_str} (Attempt {ev['attempts'] + 1})...")
                        delivered = False
                        err_msg = None

                        try:
                            if slot_id == "09:00_AGENDA":
                                delivered = await asyncio.to_thread(
                                    self.dispatcher.send_morning_tactical_briefing,
                                    shortlist=self.latest_shortlist,
                                    force=True
                                )
                            elif slot_id == "15:00_SUMMARY":
                                delivered = await asyncio.to_thread(
                                    self.dispatcher.send_end_of_trade_summary,
                                    shortlist=self.latest_shortlist,
                                    force=True
                                )
                            else: # All 30m Pulses (09:30, 10:00, 10:30, ...)
                                delivered = await asyncio.to_thread(
                                    self.dispatcher.send_30m_shortlist_pulse,
                                    self.latest_shortlist,
                                    force=True
                                )
                        except Exception as e:
                            err_msg = str(e)
                            logger.error(f"Error dispatching milestone {slot_id}: {e}")

                        if delivered:
                            self.ledger.mark_completed(date_str, slot_id, ist_now_str)
                        else:
                            self.ledger.mark_failed(date_str, slot_id, err_msg or "Dispatch returned False")

                await asyncio.sleep(5)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in Clock Dispatcher loop: {e}")
                await asyncio.sleep(10)

    async def _scanner_worker_loop(self):
        """Background 516-universe data refresher."""
        logger.info("Scanner Data Worker Loop active.")
        # Perform initial scan in background worker so start_loop does not block clock dispatcher
        try:
            await asyncio.to_thread(self.scan_universe_now)
        except Exception as e:
            logger.error(f"Error in initial universe scan: {e}")

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
        
        # Run loops concurrently immediately without blocking clock dispatcher or paper tracker
        await asyncio.gather(
            self._clock_dispatcher_loop(),
            self._scanner_worker_loop(),
            self.paper_tracker.start_polling_loop()
        )

    def stop_loop(self):
        self.is_running = False
        self.paper_tracker.stop_polling_loop()
