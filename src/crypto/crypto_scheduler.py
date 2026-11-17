"""
FinPlus+ Crypto Delta - 24/7 Background Radar Scheduler
Continuously polls Delta India market data, runs multi-timeframe analytics, and manages paper positions.
"""

import time
import asyncio
from typing import Dict, Any, List, Optional
from src.utils.time_and_logger import setup_logger, get_ist_iso, get_utc_iso
from src.crypto.delta_india_feed import DeltaIndiaFeed
from src.crypto.multi_timeframe_coiling import MultiTimeframeCoilingEngine
from src.crypto.crypto_paper_engine import CryptoPaperEngine
from src.crypto.ntfy_crypto import CryptoNTFYDispatcher
from src.crypto.crypto_config import load_contracts, CONFLUENCE_TRIGGER_THRESHOLD

logger = setup_logger("CryptoScheduler")

class CryptoScheduler:
    def __init__(self):
        self.feed = DeltaIndiaFeed()
        self.engine = MultiTimeframeCoilingEngine()
        self.paper = CryptoPaperEngine()
        self.ntfy = CryptoNTFYDispatcher()
        self.contracts = load_contracts()

        self.is_running = False
        self._loop_task: Optional[asyncio.Task] = None
        self.latest_scan_results: List[Dict[str, Any]] = []
        self.priority_trade: Optional[Dict[str, Any]] = None
        self.last_scan_ts: float = 0.0
        self.notified_triggers: Dict[str, float] = {} # Symbol -> timestamp to prevent spam

    async def start(self):
        """Starts the persistent 24/7 async scan loop."""
        if self.is_running:
            return
        self.is_running = True
        logger.info(f"Starting 24/7 Crypto Delta Radar Scheduler across {len(self.contracts)} instruments...")
        self._loop_task = asyncio.create_task(self._scan_loop())

    async def stop(self):
        """Halts the scan loop gracefully."""
        self.is_running = False
        if self._loop_task:
            self._loop_task.cancel()
        logger.info("Crypto Delta Radar Scheduler stopped.")

    async def run_single_scan(self) -> List[Dict[str, Any]]:
        """Executes a single scan cycle synchronously in threadpool."""
        return await asyncio.to_thread(self._execute_scan)

    def _execute_scan(self) -> List[Dict[str, Any]]:
        """Worker method for single scan execution."""
        tickers = self.feed.get_all_tickers()
        results = []
        live_prices = {}
        top_trade = None
        max_score = -1

        for c in self.contracts:
            sym = c["symbol"]
            ticker = tickers.get(sym, {})
            ltp = float(ticker.get("close", 0.0))
            if ltp <= 0:
                ltp = float(ticker.get("mark_price", 0.0))
            if ltp > 0:
                live_prices[sym] = ltp

            # Fetch 15m and 5m candles (200 bars for authentic 50 EMA and LuxAlgo Range Detector)
            candles_15m = self.feed.get_candles(sym, resolution="15m", count=200)
            candles_5m = self.feed.get_candles(sym, resolution="5m", count=100)

            analysis = self.engine.analyze_symbol(
                symbol=sym,
                candles_15m=candles_15m,
                candles_5m=candles_5m,
                ltp_override=ltp
            )

            # Enrich with contract specifications
            analysis["display_name"] = c.get("display_name", sym)
            analysis["category"] = c.get("category", "CRYPTO")
            analysis["contract_value"] = c.get("contract_value", 1.0)
            analysis["tick_size"] = c.get("tick_size", 0.01)
            analysis["default_lot"] = c.get("default_lot", 1)
            analysis["change_24h"] = float(ticker.get("ltp_change_24h", 0.0)) if ticker else 0.0
            analysis["high_24h"] = float(ticker.get("high", 0.0)) if ticker else 0.0
            analysis["low_24h"] = float(ticker.get("low", 0.0)) if ticker else 0.0
            analysis["volume_24h"] = float(ticker.get("volume", 0.0)) if ticker else 0.0
            analysis["funding_rate"] = float(ticker.get("funding_rate", 0.0)) if ticker else 0.0

            results.append(analysis)

            score = analysis.get("confluence_score", 0)
            if score > max_score:
                max_score = score
                top_trade = analysis

            # Automated Paper Trade Ingestion & NTFY Alerting
            if score >= CONFLUENCE_TRIGGER_THRESHOLD and analysis.get("action"):
                action = analysis["action"]
                entry = analysis.get("entry_price")
                sl = analysis.get("stop_loss")
                t1 = analysis.get("target_1")
                t2 = analysis.get("target_2")

                # Open paper position
                opened = self.paper.open_position(
                    symbol=sym,
                    action=action,
                    entry_price=entry,
                    stop_loss=sl,
                    target_1=t1,
                    target_2=t2,
                    contract_value=c.get("contract_value", 1.0),
                    default_qty=c.get("default_lot", 1),
                    notes=f"Confluence {score} pts | {analysis.get('reason', '')}"
                )

                # Send NTFY alert for Delta trade opened
                now_ts = time.time()
                last_alert = self.notified_triggers.get(sym, 0.0)
                if opened:
                    self.ntfy.send_order_opened_alert(opened)
                    self.notified_triggers[sym] = now_ts
                elif (now_ts - last_alert) >= 1800:
                    self.notified_triggers[sym] = now_ts
                    self.ntfy.send_breakout_alert(analysis)

        # Update paper trailing stop losses and check SL hits (No target cap)
        closed_trades, trailed_trades = self.paper.update_positions(live_prices)
        for ct in closed_trades:
            self.ntfy.send_trade_closed_alert(ct)
        for tt in trailed_trades:
            if tt.get("breakeven_latched") == 1 and not tt.get("breakeven_alert_sent"):
                tt["breakeven_alert_sent"] = 1
                self.ntfy.send_breakeven_trailed_alert(tt)

        # Sort results by confluence score descending
        results.sort(key=lambda r: r.get("confluence_score", 0), reverse=True)
        self.latest_scan_results = results
        self.priority_trade = top_trade
        self.last_scan_ts = time.time()

        return results

    def refresh_open_positions_live(self) -> Dict[str, Any]:
        """
        Fast real-time price refresher (<50ms).
        Queries live quotes for all currently open positions, trails SLs, and updates PnL in INR.
        """
        if not self.paper.active_positions:
            return self.paper.get_status_dict()

        tickers = self.feed.get_all_tickers()
        live_prices = {}
        for pos in self.paper.active_positions.values():
            sym = pos["symbol"]
            ticker = tickers.get(sym)
            if ticker:
                p = float(ticker.get("close", 0.0)) or float(ticker.get("mark_price", 0.0))
                if p > 0:
                    live_prices[sym] = p

        if live_prices:
            closed_trades, trailed_trades = self.paper.update_positions(live_prices)
            for ct in closed_trades:
                self.ntfy.send_trade_closed_alert(ct)
            for tt in trailed_trades:
                if tt.get("breakeven_latched") == 1 and not tt.get("breakeven_alert_sent"):
                    tt["breakeven_alert_sent"] = 1
                    self.ntfy.send_breakeven_trailed_alert(tt)

        return self.paper.get_status_dict()

    async def _scan_loop(self):
        """Internal asynchronous loop running every 45-60 seconds."""
        while self.is_running:
            try:
                await self.run_single_scan()
            except Exception as e:
                logger.error(f"Error during crypto scan loop: {e}")
            await asyncio.sleep(45)
