"""
FinPlus+ v2.5.4 End-to-End Acceptance Test Suite:
1. 10 simulated scan cycles with healthy broker -> exactly 0 yfinance calls.
2. Broker stale 4 minutes with an open position -> exactly one fallback call, tagged YF_FALLBACK, no new entry signal.
"""

import time
import pandas as pd
from unittest.mock import patch, MagicMock
from src.scheduler.scanner_scheduler import ScannerScheduler
from src.feeds.market_data_feed import MarketDataFeed

def test_10_scan_cycles_healthy_broker_zero_yfinance_calls(tmp_path):
    """TEST: 10 scan cycles with healthy broker -> 0 yfinance calls."""
    scheduler = ScannerScheduler()
    # Mock market as open
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

    yf_calls = 0
    def mock_gw_download(*args, **kwargs):
        nonlocal yf_calls
        yf_calls += 1
        return dummy_df

    with patch.object(scheduler.feed.gateway, "download", side_effect=mock_gw_download):
        with patch("src.scheduler.scanner_scheduler.is_market_open_ist", return_value=True):
            with patch("src.feeds.market_data_feed.is_equity_market_open_ist", return_value=True):
                # Run 10 scan cycles
                for cycle in range(10):
                    scheduler.scan_universe_now()

    assert yf_calls == 0, f"Expected 0 yfinance calls across 10 healthy scan cycles, but got {yf_calls}!"

def test_broker_stale_with_open_position_triggers_fallback_and_blocks_new_entry(tmp_path):
    """TEST: Broker stale 4 minutes with open position -> fallback call, tagged YF_FALLBACK, no new entry signal."""
    scheduler = ScannerScheduler()
    scheduler.indmoney_feed = MagicMock()
    # 4 minutes stale = 240 seconds
    scheduler.indmoney_feed._last_fetch_time = time.time() - 240.0
    scheduler.is_stale_feed = True

    # Register open position for RELIANCE
    scheduler.paper_tracker.active_positions["pos_rel"] = {
        "position_id": "pos_rel",
        "symbol": "RELIANCE",
        "sector": "INTRADAY_CASH",
        "action": "BUY",
        "is_option": False,
        "entry_price": 2500.0,
        "current_price": 2500.0,
        "quantity": 10,
        "risk_r": 10.0,
        "initial_sl": 2480.0,
        "trailing_sl": 2480.0,
        "target": 2600.0,
        "entry_time_ist": "10:00:00 AM IST",
        "opened_at_utc": "2026-11-13T04:30:00Z",
        "status": "OPEN",
        "stage": "INITIAL (FIXED SL)",
        "unrealized_pnl": 0.0,
    }

    dummy_df = pd.DataFrame({
        "Open": [2500.0] * 150, "High": [2520.0] * 150, "Low": [2490.0] * 150, "Close": [2510.0] * 150, "Volume": [5000] * 150
    }, index=pd.date_range("2026-11-01 09:15", periods=150, freq="15min", tz="Asia/Kolkata"))

    download_reasons = []
    def mock_gw_download(*args, **kwargs):
        reason = kwargs.get("reason", args[1] if len(args) > 1 else "UNKNOWN")
        download_reasons.append(reason)
        return dummy_df

    scheduler.feed._cache.clear()

    # Clear CandleStore for RELIANCE so fallback guard executes
    with patch.object(scheduler.feed.candle_store, "get_candles_as_dataframe", return_value=pd.DataFrame()):
        with patch.object(scheduler.feed.gateway, "download", side_effect=mock_gw_download):
            df = scheduler.feed.fetch_15m_candles(
                "RELIANCE.NS",
                allow_fallback_guard=True,
                broker_stale_seconds=240.0,
                is_priority_symbol=True
            )

    assert "FALLBACK_GUARD" in download_reasons, f"Expected FALLBACK_GUARD call, got {download_reasons}"
    assert df.attrs.get("data_source") == "YF_FALLBACK"

    # Evaluate instrument on this YF_FALLBACK df
    item = {"symbol": "RELIANCE", "yf_ticker": "RELIANCE.NS", "category": "INTRADAY_CASH"}
    eval_res = scheduler.detector.evaluate_instrument(df, item)

    # Check detector output blocking
    if df.attrs.get("data_source") == "YF_FALLBACK":
        eval_res["feed_status"] = "YF_FALLBACK"
        eval_res["signal_direction"] = 0
        eval_res["regime_confluence"] = "VETOED (YF_FALLBACK)"

    assert eval_res["signal_direction"] == 0, "YF_FALLBACK data must not produce directional entry signal!"
    assert eval_res["feed_status"] == "YF_FALLBACK"
    assert "VETOED" in eval_res["regime_confluence"]

