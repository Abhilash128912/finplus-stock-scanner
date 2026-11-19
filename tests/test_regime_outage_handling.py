"""
Unit tests for App #1 Market Regime Outage Handling:
Verifies reading data_status, skipping all new signals on UNKNOWN/STALE/PARTIAL spot or breadth,
logging SKIPPED_NO_REGIME, and dispatching exactly ONE NTFY alert per outage.
"""

import pytest
from unittest.mock import MagicMock, patch
from src.analytics.regime_interceptor import App1RegimeInterceptor


def test_check_regime_outage_matrix():
    """Verifies all outage conditions: UNKNOWN, STALE, PARTIAL (missing spot/breadth)."""
    # 1. UNKNOWN regime -> Outage
    reg_unknown = {"regime": "UNKNOWN", "data_status": "PARTIAL", "metrics": {"nifty_spot": 25000.0, "sensex_spot": 80000.0, "broad_market_advances": 1000, "advance_decline_ratio": 1.0}}
    is_out, reason = App1RegimeInterceptor.check_regime_outage(reg_unknown, True)
    assert is_out is True
    assert "REGIME_UNKNOWN" in reason

    # 2. STALE data status -> Outage
    reg_stale = {"regime": "UPTREND", "data_status": "STALE", "metrics": {"nifty_spot": 25000.0, "sensex_spot": 80000.0, "broad_market_advances": 1000, "advance_decline_ratio": 1.0}}
    is_out, reason = App1RegimeInterceptor.check_regime_outage(reg_stale, True)
    assert is_out is True
    assert "FEED_STALE" in reason

    # 3. PARTIAL with missing nifty_spot -> Outage
    reg_partial_missing = {"regime": "UPTREND", "data_status": "PARTIAL", "metrics": {"nifty_spot": None, "sensex_spot": 80000.0, "broad_market_advances": 1000, "advance_decline_ratio": 1.0}}
    is_out, reason = App1RegimeInterceptor.check_regime_outage(reg_partial_missing, True)
    assert is_out is True
    assert "FEED_PARTIAL_MISSING_DATA" in reason

    # 4. PARTIAL but spot and breadth healthy (e.g. only VIX None) -> NOT an outage
    reg_partial_vix_only = {"regime": "UPTREND", "data_status": "PARTIAL", "metrics": {"nifty_spot": 25000.0, "sensex_spot": 80000.0, "broad_market_advances": 1200, "advance_decline_ratio": 1.5, "vix_value": None}}
    is_out, reason = App1RegimeInterceptor.check_regime_outage(reg_partial_vix_only, True)
    assert is_out is False

    # 5. Full OK -> NOT an outage
    reg_ok = {"regime": "STRONG_UPTREND", "data_status": "OK", "metrics": {"nifty_spot": 25000.0, "sensex_spot": 80000.0, "broad_market_advances": 1500, "advance_decline_ratio": 2.5}}
    is_out, reason = App1RegimeInterceptor.check_regime_outage(reg_ok, True)
    assert is_out is False


def test_evaluate_confluence_vetoes_on_outage():
    """When App #1 indicates regime outage, evaluate_confluence returns VETOED_NO_REGIME."""
    interceptor = App1RegimeInterceptor()
    reg_outage = {
        "regime": "UNKNOWN",
        "data_status": "STALE",
        "trend": {"code": "UNKNOWN", "score": 0},
        "metrics": {"nifty_spot": None, "sensex_spot": None}
    }
    interceptor._cached_result = (True, reg_outage)
    interceptor._cache_ts = 9999999999.0

    status, badge, _ = interceptor.evaluate_confluence(signal_direction=1, instrument_type="STOCK", rvol=3.0, confluence_score=95)
    assert status == "VETOED_NO_REGIME"
    assert "SKIPPED_NO_REGIME" in badge


def test_one_ntfy_alert_per_outage_not_per_cycle(caplog):
    """
    Verifies that ScannerScheduler dispatches exactly ONE NTFY alert per outage
    across multiple consecutive scan cycles, and resets when regime recovers.
    """
    from src.scheduler.scanner_scheduler import ScannerScheduler

    with patch.object(ScannerScheduler, "__init__", lambda self: None):
        scheduler = ScannerScheduler()
        scheduler.dispatcher = MagicMock()
        scheduler.interceptor = MagicMock()
        scheduler.feed = MagicMock()
        scheduler.indmoney_feed = MagicMock()
        scheduler.indmoney_feed.get_cached_quotes.return_value = {}
        scheduler.universe = []
        scheduler.priority_symbols = []
        scheduler.fallback_active = False
        scheduler.is_stale_feed = False
        scheduler.consecutive_good_broker_cycles = 0
        scheduler.indmoney_feed._last_fetch_time = 0.0
        scheduler.shadow_variants = {}
        scheduler.detector = MagicMock()
        scheduler.shortlist_engine = MagicMock()
        scheduler.paper_tracker = MagicMock()
        scheduler.ledger = MagicMock()

    outage_payload = {
        "regime": "UNKNOWN",
        "data_status": "STALE",
        "trend": {"code": "UNKNOWN"},
        "metrics": {"nifty_spot": None, "sensex_spot": None, "broad_market_advances": None, "advance_decline_ratio": None}
    }
    recovered_payload = {
        "regime": "UPTREND",
        "data_status": "OK",
        "trend": {"code": "UPTREND", "score": 35},
        "metrics": {"nifty_spot": 25200.0, "sensex_spot": 82000.0, "broad_market_advances": 1400, "advance_decline_ratio": 2.0}
    }

    # Cycle 1: Outage begins -> should send 1 alert
    scheduler.interceptor.fetch_current_regime.return_value = (True, outage_payload)
    scheduler.interceptor.check_regime_outage.return_value = (True, "FEED_STALE")

    with caplog.at_level("WARNING"):
        scheduler.scan_universe_now()

    assert scheduler.dispatcher.send_regime_outage_alert.call_count == 1
    assert scheduler._regime_outage_alerted is True
    assert "SKIPPED_NO_REGIME" in caplog.text

    # Cycle 2: Outage persists -> should NOT send another alert
    caplog.clear()
    scheduler.scan_universe_now()
    assert scheduler.dispatcher.send_regime_outage_alert.call_count == 1  # still 1!
    assert "SKIPPED_NO_REGIME" in caplog.text

    # Cycle 3: Outage clears -> alert state resets
    scheduler.interceptor.fetch_current_regime.return_value = (True, recovered_payload)
    scheduler.interceptor.check_regime_outage.return_value = (False, "")
    scheduler.scan_universe_now()
    assert scheduler._regime_outage_alerted is False
    assert scheduler.dispatcher.send_regime_outage_alert.call_count == 1

    # Cycle 4: New outage begins -> sends second alert
    scheduler.interceptor.fetch_current_regime.return_value = (True, outage_payload)
    scheduler.interceptor.check_regime_outage.return_value = (True, "REGIME_UNKNOWN")
    scheduler.scan_universe_now()
    assert scheduler.dispatcher.send_regime_outage_alert.call_count == 2
    assert scheduler._regime_outage_alerted is True
