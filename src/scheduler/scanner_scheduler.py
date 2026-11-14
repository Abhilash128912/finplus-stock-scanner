"""
FinPlus+ Scanner Scheduler & Continuous Evaluation Loop
Orchestrates data fetching, closed-candle LuxAlgo range calculation,
fail-closed App #1 confluence check, Shortlist Engine evaluation,
and strict 30-minute NTFY pulse dispatching.
"""

import time
import asyncio
from typing import Dict, Any, List, Optional, Tuple, Set
from src.feeds.market_data_feed import MarketDataFeed
from src.analytics.range_detector_engine import RangeDetectorEngine
from src.analytics.regime_interceptor import App1RegimeInterceptor
from src.analytics.shortlist_engine import ShortlistEngine
from src.analytics.multi_setup_engine import MultiSetupEngine
from src.notifications.ntfy_dispatcher import NTFYDispatcher
from src.paper.live_paper_tracker import LivePaperTracker
from src.paper.indmoney_live_feed import INDMoneyLiveFeed
from src.storage.schedule_ledger import ScheduleLedger
from src.storage.candle_store import CandleStore
from src.storage.firestore_backend import FirestoreBackend
from src.storage.bar_parity_recorder import BarParityRecorder
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
        # Live detector uses ATR 100 as temporary value per Step 1 requirement (ATR 500 cannot be supported without 522 bars in cache)
        self.detector = RangeDetectorEngine(length=20, mult=1.0, atr_len=100)
        logger.info("[ScannerScheduler] LIVE range detector initialized with ATR 100 (min_history_bars=122).")

        # Shadow Variants (Step 4 & Amendment): side-by-side evaluation matrix
        self.shadow_variants = {
            "ATR_14_W5": RangeDetectorEngine(length=20, mult=1.0, atr_len=14, break_window_bars=5),
            "ATR_50_W5": RangeDetectorEngine(length=20, mult=1.0, atr_len=50, break_window_bars=5),
            "ATR_100_W5": self.detector, # Live baseline
            "ATR_500_W5": RangeDetectorEngine(length=20, mult=1.0, atr_len=500, break_window_bars=5),
            "ATR_LEGACY_HALF": RangeDetectorEngine(length=20, mult=1.0, atr_len=500, break_window_bars=5, legacy_half_mode=True),
            "ATR_100_W1": RangeDetectorEngine(length=20, mult=1.0, atr_len=100, break_window_bars=1),
            "ATR_100_W2": RangeDetectorEngine(length=20, mult=1.0, atr_len=100, break_window_bars=2),
        }
        self.live_hold_buffer: Dict[str, Tuple[float, float, int]] = {} # symbol -> (price, trigger_ts, direction)
        self.MAX_CHASE_PCT = 0.70 # Lateness threshold (log only)
        self.interceptor = App1RegimeInterceptor(timeout_secs=2.0)
        self.shortlist_engine = ShortlistEngine()
        self.multi_setup_engine = MultiSetupEngine()
        self.dispatcher = NTFYDispatcher(min_interval_seconds=1800) # Strict 30 mins
        self.paper_tracker = LivePaperTracker()
        self.indmoney_feed = INDMoneyLiveFeed(cache_ttl_seconds=5.0)
        self.ledger = ScheduleLedger()
        self.candle_store = CandleStore()
        self.firestore = FirestoreBackend()
        self.parity_recorder = BarParityRecorder()
        if hasattr(self.paper_tracker, "set_firestore_backend"):
            self.paper_tracker.set_firestore_backend(self.firestore)
        else:
            self.paper_tracker.firestore = self.firestore
        
        self.latest_scan_results: List[Dict[str, Any]] = []
        self.latest_shortlist: Dict[str, Any] = {}
        self.last_scan_utc: str = get_utc_iso()
        self.is_running: bool = False
        self._dispatched_slots: set = set()
        self.is_stale_feed: bool = False
        self._last_proactive_refresh_date: Optional[str] = None
        self.consecutive_good_broker_cycles: int = 0
        self.fallback_active: bool = False
        self.last_fallback_batch_time: float = 0.0
        self.legacy_yahoo_scan_path_enabled: bool = False # Safety valve config flag

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

        # Phase 2C: Build 15m and 5m bars from INDmoney quotes with extreme wick correction
        for q_sym, q_data in ind_quotes_map.items():
            try:
                ltp = float(q_data.get("ltp", 0.0))
                if ltp > 0:
                    dh = float(q_data.get("day_high", ltp))
                    dl = float(q_data.get("day_low", ltp))
                    self.candle_store.update_live_candle_bar(q_sym, "15m", ltp, day_high=dh, day_low=dl)
                    self.candle_store.update_live_candle_bar(q_sym, "5m", ltp, day_high=dh, day_low=dl)
            except Exception:
                pass

        # Phase 2C: Bar Parity Comparison for completed bars
        if hasattr(self, "parity_recorder") and self.parity_recorder:
            try:
                for item in universe:
                    s = item.get("symbol", "").split(" ")[0].upper()
                    yt = item.get("yf_ticker")
                    if not s or not yt:
                        continue
                    yf_entry = self.feed.yahoo_parity_cache.get(yt)
                    if not yf_entry or 'df' not in yf_entry:
                        continue
                    yf_df = yf_entry['df']
                    if yf_df.empty:
                        continue
                    ind_bars = self.candle_store.get_candles(s, "15m", limit=3)
                    if len(ind_bars) >= 2:
                        prev_completed_ind = ind_bars[-2]
                        bar_epoch = prev_completed_ind.get("time")
                        matching_row = None
                        for row_idx, row in yf_df.iterrows():
                            re = int(row_idx.timestamp()) if hasattr(row_idx, "timestamp") else 0
                            if abs(re - bar_epoch) < 60:
                                matching_row = row
                                break
                        if matching_row is not None:
                            self.parity_recorder.record_bar_comparison(
                                s, "15m", bar_epoch, prev_completed_ind, matching_row.to_dict()
                            )
            except Exception as parity_err:
                logger.debug(f"Bar parity comparison notice: {parity_err}")

        # Track broker health and consecutive good cycles
        if is_open and self.indmoney_feed:
            feed_age = time.time() - getattr(self.indmoney_feed, "_last_fetch_time", 0.0)
            if feed_age <= 60.0 and len(ind_quotes_map) > 0:
                self.consecutive_good_broker_cycles += 1
                if self.is_stale_feed:
                    self.is_stale_feed = False
                    logger.info("INDmoney feed restored: broker quotes fresh and responsive.")
                # Fallback ends after 2 consecutive good broker cycles
                if self.fallback_active and self.consecutive_good_broker_cycles >= 2:
                    self.fallback_active = False
                    if hasattr(self.feed, "gateway"):
                        self.feed.gateway.fallback_active = False
                    logger.info("FALLBACK_GUARD DEACTIVATED: broker healthy for 2 consecutive cycles.")
                    self.dispatcher.send_fallback_guard_alert("END", "Broker recovered with fresh quotes.")
            else:
                self.consecutive_good_broker_cycles = 0
                if feed_age > 60.0:
                    self.is_stale_feed = True
                    logger.warning(f"Feed data is {int(feed_age)}s old (>60s). Feed marked STALE_FEED.")

        # Determine active open symbols and top-3 priority symbols for fallback guard
        open_symbols = set()
        if hasattr(self, "paper_tracker") and self.paper_tracker:
            open_symbols = {p["symbol"].split()[0].upper() for p in self.paper_tracker.active_positions.values()}

        top3_symbols = set()
        if hasattr(self, "latest_shortlist") and self.latest_shortlist:
            for k in ["best_intraday_stock", "best_stock_option", "sensex_radar", "nifty_radar"]:
                it = self.latest_shortlist.get(k)
                if it and it.get("symbol"):
                    top3_symbols.add(it["symbol"].split()[0].upper())

        priority_symbols = open_symbols | top3_symbols
        feed_age_sec = (time.time() - getattr(self.indmoney_feed, "_last_fetch_time", 0.0)) if self.indmoney_feed else 999.0

        # Check if fallback guard is triggering for the first time
        if feed_age_sec > 180.0 and len(priority_symbols) > 0 and not self.fallback_active:
            self.fallback_active = True
            if hasattr(self.feed, "gateway"):
                self.feed.gateway.fallback_active = True
            logger.warning(f"FALLBACK_GUARD ACTIVATED: broker stale {int(feed_age_sec)}s (>180s) on priority symbols {priority_symbols}")
            self.dispatcher.send_fallback_guard_alert("START", f"Broker stale {int(feed_age_sec)}s. Prioritizing {len(priority_symbols)} symbols.")

        # Daily Funnel Metrics & Trigger Candidate Collection (Phase 6c, 6d)
        funnel_counts = {
            "total_symbols_scanned": len(universe),
            "candidates_detected": 0,
            "removed_insufficient_history": 0,
            "removed_stale_feed": 0,
            "removed_data_suspect": 0,
            "removed_regime_veto": 0,
            "removed_late": 0,
            "removed_wide_sl": 0,
            "suggestions_opened_on_panel": 0,
            "ntfy_triggers_sent": 0
        }
        candidate_triggers: List[Dict[str, Any]] = []

        for item in universe:
            ticker = item.get("yf_ticker")
            sym = item.get("symbol")
            is_priority = (sym.upper() in priority_symbols)
            try:
                # Use INDmoney/CandleStore first; only allow FALLBACK_GUARD on priority symbols when broker stale > 180s
                df = self.feed.fetch_15m_candles(
                    ticker,
                    period="5d",
                    allow_fallback_guard=True,
                    broker_stale_seconds=feed_age_sec,
                    is_priority_symbol=is_priority
                )
                if df.empty:
                    funnel_counts["removed_insufficient_history"] += 1
                    continue
                    
                eval_res = self.detector.evaluate_instrument(df, item)
                if eval_res.get("status") == "INSUFFICIENT_DATA":
                    funnel_counts["removed_insufficient_history"] += 1
                    continue

                # Fallback guard rule: Any detector output using YF_FALLBACK prices must NOT create a new entry signal
                if df.attrs.get("data_source") == "YF_FALLBACK":
                    eval_res["feed_status"] = "YF_FALLBACK"
                    eval_res["signal_direction"] = 0
                    eval_res["regime_confluence"] = "VETOED (YF_FALLBACK)"
                    logger.warning(f"Symbol {sym} evaluated with YF_FALLBACK data. Blocked new entry signal.")

                # If feed is stale, tag signal STALE_FEED and block new directional signals
                if self.is_stale_feed:
                    eval_res["feed_status"] = "STALE_FEED"
                    eval_res["signal_direction"] = 0  # Block new trade signal
                    eval_res["regime_confluence"] = "VETOED (STALE_FEED)"
                
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
                        # Direct yfinance call removed per v2.5.4 gateway mandate.
                        pass
                except Exception:
                    pass
                
                # Step 4 (v2.6.0 SHADOW MODE): Evaluate shadow variants and record into range_variants table
                import datetime
                today_ist_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
                for var_name, var_detector in self.shadow_variants.items():
                    if var_name == "ATR_100_W5":
                        shadow_res = eval_res
                    else:
                        shadow_res = var_detector.evaluate_instrument(df, item)

                    sh_sig = shadow_res.get("signal_direction", 0)
                    sh_top = shadow_res.get("range_top")
                    sh_bot = shadow_res.get("range_bottom")
                    sh_mid = shadow_res.get("range_mid")
                    sh_live_p = eval_res.get("live_ltp", eval_res.get("last_price", 0.0))

                    # Lateness computation: % above range top (or below range bot)
                    lat_pct = 0.0
                    lat_block = False
                    if sh_sig == 1 and sh_top and sh_top > 0 and sh_live_p > sh_top:
                        lat_pct = round(((sh_live_p - sh_top) / sh_top) * 100.0, 2)
                        lat_block = (lat_pct > self.MAX_CHASE_PCT)
                    elif sh_sig == -1 and sh_bot and sh_bot > 0 and sh_live_p < sh_bot:
                        lat_pct = round(((sh_bot - sh_live_p) / sh_bot) * 100.0, 2)
                        lat_block = (lat_pct > self.MAX_CHASE_PCT)

                    if lat_block:
                        logger.info(f"[SHADOW_LATENESS_GATE] {var_name} on {sym}: lateness {lat_pct}% exceeds {self.MAX_CHASE_PCT}%. (LOG ONLY - NOT BLOCKED)")

                    # Record to range_variants table
                    self.ledger.record_range_variant_eval({
                        "date_str": today_ist_str,
                        "symbol": sym,
                        "variant_name": var_name,
                        "timeframe": "15m",
                        "atr_len_configured": shadow_res.get("atr_len_configured", var_detector.atr_len),
                        "atr_len_effective": shadow_res.get("atr_len_effective", var_detector.atr_len),
                        "range_len": var_detector.length,
                        "break_window_bars": var_detector.break_window_bars,
                        "range_top": sh_top,
                        "range_bottom": sh_bot,
                        "range_mid": sh_mid,
                        "live_price": sh_live_p,
                        "signal_direction": sh_sig,
                        "lateness_pct": lat_pct,
                        "lateness_would_block": lat_block,
                        "confluence_score": shadow_res.get("confluence_score", 0),
                        "status": shadow_res.get("status", "EVALUATED")
                    })

                # Cross-reference with App #1 (Fail-Closed 2s Timeout)
                sig_dir = eval_res.get("signal_direction", 0)
                conf_status, conf_badge, regime_data = self.interceptor.evaluate_confluence(
                    sig_dir, item.get("category", "STOCK"), eval_res.get("rvol", 1.0), eval_res.get("confluence_score", 0)
                )
                
                # If feed is stale, override regime confluence and tag STALE_FEED
                if self.is_stale_feed:
                    eval_res["feed_status"] = "STALE_FEED"
                    eval_res["signal_direction"] = 0
                    conf_status = "VETOED (STALE_FEED)"

                eval_res["regime_confluence"] = conf_status
                eval_res["regime_badge"] = conf_badge
                eval_res["regime_data"] = regime_data
                results.append(eval_res)

                # Multi-Setup Evaluation (Phase 6b) & Two-Stage Trigger Detection (Phase 5d)
                if hasattr(self, "multi_setup_engine") and self.multi_setup_engine:
                    try:
                        setups = self.multi_setup_engine.evaluate_setups(df, eval_res, item)
                        for cand in setups:
                            funnel_counts["candidates_detected"] += 1
                            if cand.get("stage_type") == "WATCH":
                                now_ts = time.time()
                                last_w = self.multi_setup_engine.last_watch_alert_time.get(sym, 0.0)
                                if (now_ts - last_w) >= 600.0:
                                    self.multi_setup_engine.last_watch_alert_time[sym] = now_ts
                                    self.dispatcher.send_watch_alert(
                                        symbol=sym,
                                        setup_name=cand["setup_name"],
                                        boundary=cand["boundary"],
                                        current_price=cand["entry_price"],
                                        rvol=cand["rvol"]
                                    )
                            elif cand.get("stage_type") == "TRIGGER":
                                if cand.get("is_late"):
                                    funnel_counts["removed_late"] += 1
                                    continue
                                if "VETOED" in cand.get("regime_confluence", ""):
                                    funnel_counts["removed_regime_veto"] += 1
                                    continue
                                if self.paper_tracker and sym in getattr(self.paper_tracker, "suspect_symbols", set()):
                                    funnel_counts["removed_data_suspect"] += 1
                                    continue
                                if self.is_stale_feed:
                                    funnel_counts["removed_stale_feed"] += 1
                                    continue

                                grade = self.paper_tracker.get_setup_grade(cand["setup_key"]) if self.paper_tracker else "B"
                                cand["grade"] = grade
                                cand["rank_score"] = self.multi_setup_engine.score_candidate(cand, grade=grade)
                                cand["atr_val"] = eval_res.get("atr_val")
                                candidate_triggers.append(cand)
                    except Exception as setup_err:
                        logger.debug(f"Multi-setup evaluation notice for {sym}: {setup_err}")

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

        # Multi-Setup Menu Selection & Paper Placement (Phase 6b, 6c)
        candidate_triggers.sort(key=lambda x: x.get("rank_score", 0.0), reverse=True)
        if self.paper_tracker:
            for cand in candidate_triggers:
                taken_sum = self.paper_tracker.get_taken_summary()
                # Target: 6 to 12 suggestions per day opened on the panel (Phase 6c)
                if taken_sum.get("suggestions_today", 0) >= 12:
                    break

                if cand.get("is_wide_sl"):
                    funnel_counts["removed_wide_sl"] += 1

                opened = self.paper_tracker.open_position(
                    source_app="APP_02_STOCK_SCANNER",
                    sector=cand["sector"],
                    symbol=cand["symbol"],
                    action=cand["action"],
                    entry_price=cand["entry_price"],
                    stop_loss=cand["stop_loss"],
                    target=0.0,
                    quantity=None, # Automatically sized to 20% of virtual capital (Phase 5c)
                    notes=cand["notes"],
                    setup_key=cand["setup_key"],
                    atr_val=cand.get("atr_val")
                )
                if opened:
                    funnel_counts["suggestions_opened_on_panel"] += 1

            funnel_counts["ntfy_triggers_sent"] = getattr(self.paper_tracker, "daily_ntfy_triggers_count", 0)
            self.paper_tracker.record_funnel_metrics(funnel_counts)
        
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

        # Check fallback ratio: If >10% of tickers fell back to CandleStore in one cycle, alert via NTFY (priority, max once/hour)
        if self.feed and hasattr(self.feed, "fallback_tickers_this_cycle"):
            fb_count = len(self.feed.fallback_tickers_this_cycle)
            tot_count = max(1, len(all_tickers))
            fb_ratio = fb_count / tot_count
            now_ts = time.time()
            if fb_ratio > 0.10 and (now_ts - getattr(self.feed, "last_fallback_alert_time", 0.0)) > 3600.0:
                self.feed.last_fallback_alert_time = now_ts
                logger.warning(f"HIGH FALLBACK RATIO: {fb_count}/{tot_count} ({fb_ratio:.1%}) tickers using CandleStore fallback.")
                for topic in self.dispatcher.topics:
                    self.dispatcher._safe_post(
                        f"https://ntfy.sh/{topic}",
                        f"Yahoo Finance degraded: {fb_count}/{tot_count} ({fb_ratio:.1%}) tickers falling back to CandleStore.",
                        {"Title": "FinPlus Feed Alert | CandleStore Fallback", "Priority": "4"},
                        alert_type="ROUTINE",
                        is_priority=True
                    )
            # Reset fallback set for next cycle
            self.feed.fallback_tickers_this_cycle.clear()
        
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
                date_str = ist_now.strftime("%Y-%m-%d")
                ist_now_str = ist_now.strftime("%I:%M %p IST")

                # Check for official NSE Holiday
                is_hol, hol_name = is_nse_holiday(ist_now.date())
                is_weekend = (ist_now.weekday() >= 5)

                if is_hol or is_weekend:
                    reason = f"Weekend ({ist_now.strftime('%A')})" if is_weekend else f"NSE Holiday ({hol_name})"
                    if date_str != self._last_proactive_refresh_date:
                        self._last_proactive_refresh_date = date_str
                        logger.info(f"Market closed today: {reason}")

                    if is_hol and not is_weekend:
                        slot_hol = f"{date_str}_holiday_alert"
                        if ist_now.hour >= 9 and slot_hol not in self._dispatched_slots:
                            self._dispatched_slots.add(slot_hol)
                            logger.info(f"NSE Holiday Gate: Today is {hol_name}. Dispatching holiday notice...")
                            self.dispatcher.send_holiday_notice(hol_name)
                    await asyncio.sleep(60)
                    continue

                # Weekdays only on open trading days:
                # Proactive Token Refresh at 08:45 AM IST on trading days
                if (ist_now.hour == 8 and ist_now.minute >= 45) or (ist_now.hour >= 9):
                    if self._last_proactive_refresh_date != date_str:
                        self._last_proactive_refresh_date = date_str
                        logger.info(f"08:45 IST Proactive Token Warmup triggered for trading day {date_str}...")
                        if self.indmoney_feed and self.indmoney_feed.auth_client:
                            try:
                                await asyncio.to_thread(self.indmoney_feed.auth_client.get_valid_token, auto_refresh=True)
                                logger.info("08:45 IST Proactive Token Warmup successful.")
                                self.is_stale_feed = False
                            except Exception as auth_err:
                                logger.error(f"08:45 IST Proactive Token Warmup failed: {auth_err}. Marking feed STALE_FEED.")
                                self.is_stale_feed = True
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
                            elif slot_id == "14:00_FUNNEL_CHECK":
                                # Funnel shortfall check (Phase 6d): if trades opened < 6, alert top 3 removal reasons
                                funnel = self.paper_tracker.get_funnel_summary(date_str) if self.paper_tracker else {}
                                opened_count = funnel.get("trades_opened", 0)
                                if opened_count < 6:
                                    removal_counts = [
                                        ("Midday Freeze", funnel.get("removed_midday_freeze", 0)),
                                        ("Regime Veto", funnel.get("removed_regime_veto", 0)),
                                        ("Late Price Jump", funnel.get("removed_late", 0)),
                                        ("Insufficient History", funnel.get("removed_insufficient_history", 0)),
                                        ("Stale Feed", funnel.get("removed_stale_feed", 0)),
                                        ("Data Suspect", funnel.get("removed_data_suspect", 0)),
                                    ]
                                    removal_counts.sort(key=lambda x: x[1], reverse=True)
                                    top_3 = [r for r in removal_counts if r[1] > 0][:3]
                                    delivered = await asyncio.to_thread(
                                        self.dispatcher.send_funnel_shortfall_alert,
                                        opened_count, top_3
                                    )
                                else:
                                    delivered = True
                            elif slot_id == "18:00_WEEKLY_REPORT":
                                weekly_stats = self.paper_tracker.get_weekly_performance_report() if self.paper_tracker else {}
                                delivered = await asyncio.to_thread(
                                    self.dispatcher.send_sunday_weekly_report,
                                    weekly_stats
                                )
                            elif slot_id == "15:00_SUMMARY":
                                rej_tot = 0
                                if self.paper_tracker and hasattr(self.paper_tracker, "get_rejected_ticks_telemetry"):
                                    rej_tot = self.paper_tracker.get_rejected_ticks_telemetry().get("total_rejected_ticks", 0)
                                delivered = await asyncio.to_thread(
                                    self.dispatcher.send_end_of_trade_summary,
                                    shortlist=self.latest_shortlist,
                                    force=True,
                                    rejected_ticks_total=rej_tot
                                )
                            elif slot_id == "15:45_SELF_REPORT":
                                rej_tot = 0
                                if self.paper_tracker and hasattr(self.paper_tracker, "get_rejected_ticks_telemetry"):
                                    rej_tot = self.paper_tracker.get_rejected_ticks_telemetry().get("total_rejected_ticks", 0)
                                parity_sum = self.parity_recorder.get_daily_summary(date_str) if hasattr(self, "parity_recorder") else {}
                                fb_sum = self.firestore.get_telemetry() if hasattr(self, "firestore") else {}
                                delivered = await asyncio.to_thread(
                                    self.dispatcher.send_1545_self_report,
                                    rejected_ticks_total=rej_tot,
                                    force=True,
                                    parity_summary=parity_sum,
                                    firebase_summary=fb_sum
                                )
                                # Post-market candle sync to Firestore
                                if hasattr(self, "firestore") and self.firestore.is_connected:
                                    logger.info("15:45 Post-Market: Uploading daily candle history to Firestore...")
                                    asyncio.create_task(asyncio.to_thread(self.firestore.upload_all_candles_from_sqlite, self.candle_store))
                            elif slot_id in ("08:30_HISTORY_BACKFILL", "16:00_HISTORY_BACKFILL"):
                                # Run off-hours history backfill runner via YFinanceGateway with HISTORY_BACKFILL
                                logger.info(f"Triggering scheduled off-hours backfill runner ({slot_id})...")
                                universe = self.feed.get_all_universe_items() if self.feed else []
                                all_t = [it.get("yf_ticker") for it in universe if it.get("yf_ticker")]
                                cnt = await asyncio.to_thread(self.feed.prefetch_batch_15m_candles, all_t, period="60d", chunk_size=20)
                                logger.info(f"Off-hours backfill finished: updated {cnt} symbols in CandleStore.")
                                delivered = True
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

    async def run_paced_history_backfill(self, target_coverage: float = 0.90, chunk_size: int = 30, pause_seconds: float = 15.0):
        """
        Background paced history backfill.
        If universe coverage is under 90%, downloads missing symbols in paced chunks (15-20s pause)
        via YFinanceGateway with reason="HISTORY_BACKFILL".
        Runs non-blocking alongside live scans, persisting progress so restarts resume automatically.
        """
        try:
            universe = self.feed.get_all_universe_items() if self.feed else []
            if not universe:
                return

            universe_tickers = {}
            for item in universe:
                sym = item.get("symbol", "").split(" ")[0].upper()
                ticker = item.get("yf_ticker", f"{sym}.NS")
                if sym and ticker:
                    universe_tickers[sym] = ticker

            all_symbols = list(universe_tickers.keys())
            target_bars = 522 # ATR 500

            with self.candle_store._get_connection() as conn:
                rows = conn.execute("SELECT symbol, COUNT(*) FROM candles WHERE timeframe = '15m' GROUP BY symbol").fetchall()
            counts = {r[0].upper(): r[1] for r in rows}

            covered_count = sum(1 for s in all_symbols if counts.get(s, 0) >= target_bars)
            cov_ratio = (covered_count / len(all_symbols)) if all_symbols else 1.0

            if cov_ratio >= target_coverage:
                logger.info(f"STARTUP BACKFILL: Coverage is already {cov_ratio*100:.1f}% (>= {target_coverage*100:.0f}%). No backfill needed.")
                return

            missing_symbols = [s for s in all_symbols if counts.get(s, 0) < target_bars]
            logger.info(
                f"STARTUP BACKFILL ACTIVATED: Coverage {cov_ratio*100:.1f}% is below target {target_coverage*100:.0f}%. "
                f"{len(missing_symbols)} symbols to backfill in chunks of {chunk_size} (pacing {pause_seconds}s). "
                "Legacy scan path remains ON to serve market data meanwhile."
            )

            chunks = [missing_symbols[i:i + chunk_size] for i in range(0, len(missing_symbols), chunk_size)]
            for idx, chunk_syms in enumerate(chunks, 1):
                if not self.is_running:
                    break

                chunk_tickers = [universe_tickers[s] for s in chunk_syms]
                raw_df = await asyncio.to_thread(
                    self.feed.gateway.download,
                    chunk_tickers,
                    reason="HISTORY_BACKFILL",
                    caller="ScannerScheduler.startup_backfill",
                    trigger_detail=f"Startup chunk {idx}/{len(chunks)}",
                    period="30d",
                    interval="15m",
                    group_by="ticker",
                    threads=True
                )

                if raw_df is not None and not raw_df.empty:
                    if len(chunk_tickers) == 1:
                        sym = chunk_syms[0]
                        df = raw_df.copy()
                        if isinstance(df.columns, pd.MultiIndex):
                            df.columns = df.columns.get_level_values(0)
                        self.candle_store.save_dataframe_candles(sym, "15m", df)
                    else:
                        for sym, t in zip(chunk_syms, chunk_tickers):
                            try:
                                if hasattr(raw_df.columns, "levels") and t in raw_df.columns.levels[0]:
                                    df = raw_df[t].dropna(how="all").copy()
                                    if not df.empty:
                                        self.candle_store.save_dataframe_candles(sym, "15m", df)
                            except Exception:
                                pass

                # Check interim coverage
                with self.candle_store._get_connection() as conn:
                    rows = conn.execute("SELECT symbol, COUNT(*) FROM candles WHERE timeframe = '15m' GROUP BY symbol").fetchall()
                counts = {r[0].upper(): r[1] for r in rows}
                cur_cov = sum(1 for s in all_symbols if counts.get(s, 0) >= target_bars)
                logger.info(f"[STARTUP BACKFILL] Chunk {idx}/{len(chunks)} saved. Universe coverage: {cur_cov}/{len(all_symbols)} ({cur_cov/len(all_symbols)*100:.1f}%)")

                if idx < len(chunks):
                    await asyncio.sleep(pause_seconds)

            logger.info("STARTUP BACKFILL COMPLETED.")
        except Exception as err:
            logger.error(f"Error in startup backfill task: {err}")

    async def _scanner_worker_loop(self):
        """Background universe data refresher with startup history sanity check and Firestore restore."""
        logger.info("Scanner Data Worker Loop active.")
        
        # Phase 2B: Restore from Firestore if local DB is empty/cold
        if hasattr(self, "firestore") and self.firestore.is_connected:
            try:
                with self.candle_store._get_connection() as conn:
                    bar_cnt = conn.execute("SELECT COUNT(*) FROM candles").fetchone()[0]
                if bar_cnt < 500:
                    logger.info("Cold start detected (bars < 500). Restoring database state from Firestore...")
                    restore_res = await asyncio.to_thread(
                        self.firestore.restore_all_from_firestore,
                        self.candle_store, self.paper_tracker, self.ledger
                    )
                    logger.info(f"Firestore restore completed: {restore_res}")
            except Exception as restore_err:
                logger.error(f"Error during Firestore startup restore: {restore_err}")

        # Launch paced history backfill in background if coverage < 90%
        asyncio.create_task(self.run_paced_history_backfill())

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
