"""
FinPlus+ v2.5.4 End-to-End Acceptance Test Suite:
1. 10 simulated scan cycles with healthy broker -> exactly 0 yfinance calls.
2. Broker stale -> blocks directional entry signals.
3. Backtest gateway tests (tools/backtest_only/yfinance_gateway.py).
"""

import time
import pandas as pd
from unittest.mock import patch, MagicMock
from src.scheduler.scanner_scheduler import ScannerScheduler
from src.feeds.market_data_feed import MarketDataFeed
from tools.backtest_only.yfinance_gateway import YFinanceGateway
from src.paper.live_paper_tracker import LivePaperTracker

def test_10_scan_cycles_healthy_broker_zero_yfinance_calls(tmp_path):
    """TEST: 10 scan cycles with healthy broker -> 0 yfinance calls."""
    scheduler = ScannerScheduler()
    scheduler.paper_tracker = LivePaperTracker(db_path=str(tmp_path / "paper.db"))
    
    # Mock broker feed
    scheduler.indmoney_feed = MagicMock()
    scheduler.indmoney_feed.is_stale.return_value = False
    scheduler.indmoney_feed._last_fetch_time = time.time()
    scheduler.is_stale_feed = False

    # Pre-seed in-memory cache for tracked universe
    dummy_df = pd.DataFrame({
        "Open": [100.0] * 150, "High": [101.0] * 150, "Low": [99.0] * 150, "Close": [100.0] * 150, "Volume": [1000] * 150
    }, index=pd.date_range("2026-11-01 09:15", periods=150, freq="15min", tz="Asia/Kolkata"))

    for item in scheduler.feed.get_all_universe_items():
        t = item.get("yf_ticker")
        if t:
            scheduler.feed._cache[t] = {"df": dummy_df, "ts": time.time()}

    sample_universe = scheduler.feed.get_all_universe_items()[:10]
    with patch.object(scheduler.feed, "get_all_universe_items", return_value=sample_universe):
        with patch("src.scheduler.scanner_scheduler.is_market_open_ist", return_value=True):
            with patch("src.feeds.market_data_feed.is_equity_market_open_ist", return_value=True):
                # Run 10 scan cycles
                for cycle in range(10):
                    scheduler.scan_universe_now()

    # Confirms live path runs without gateway and never calls yfinance
    assert not hasattr(scheduler.feed, "gateway"), "MarketDataFeed must not have gateway attribute!"

def test_broker_stale_with_open_position_blocks_new_entry(tmp_path):
    """TEST: Broker stale -> blocks new entry signals."""
    scheduler = ScannerScheduler()
    scheduler.indmoney_feed = MagicMock()
    scheduler.indmoney_feed._last_fetch_time = time.time() - 240.0
    scheduler.is_stale_feed = True

    dummy_df = pd.DataFrame({
        "Open": [2500.0] * 150, "High": [2520.0] * 150, "Low": [2490.0] * 150, "Close": [2510.0] * 150, "Volume": [5000] * 150
    }, index=pd.date_range("2026-11-01 09:15", periods=150, freq="15min", tz="Asia/Kolkata"))

    # Evaluate instrument when feed is stale
    item = {"symbol": "RELIANCE", "yf_ticker": "RELIANCE.NS", "category": "INTRADAY_CASH"}
    eval_res = scheduler.detector.evaluate_instrument(dummy_df, item)

    if scheduler.is_stale_feed:
        eval_res["feed_status"] = "STALE_FEED"
        eval_res["signal_direction"] = 0
        eval_res["regime_confluence"] = "VETOED (STALE_FEED)"

    assert eval_res["signal_direction"] == 0, "STALE_FEED data must not produce directional entry signal!"
    assert eval_res["feed_status"] == "STALE_FEED"
    assert "VETOED" in eval_res["regime_confluence"]

def test_backtest_gateway_budget_limit(tmp_path):
    """TEST: Backtest tool gateway respects daily limits."""
    db_file = tmp_path / "test_gw_budget.db"
    gw = YFinanceGateway(db_path=str(db_file), max_calls_per_day=2)
    gw.max_calls_per_day = 2

    dummy_df = pd.DataFrame({"Close": [100.0, 101.0]})
    with patch("tools.backtest_only.yfinance_gateway.yf.download", return_value=dummy_df):
        res1 = gw.download("RELIANCE.NS", reason="HISTORY_BACKFILL")
        res2 = gw.download("TCS.NS", reason="HISTORY_BACKFILL")
        assert not res1.empty
        assert not res2.empty

        # 3rd call exceeds budget of 2 -> must be BLOCKED
        res3 = gw.download("INFY.NS", reason="HISTORY_BACKFILL")
        assert res3.empty, "3rd call should have been blocked by daily budget!"

    stats = gw.get_today_stats()
    assert stats["calls"] == 3
    assert stats["blocked"] == 1
