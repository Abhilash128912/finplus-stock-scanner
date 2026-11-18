"""
Unit & Integration Test Suite for INDstocks Real Option Quotes
Module: tests.test_indstocks_option_quotes
Tests:
1. OptionQuoteProvider interface & IndstocksQuoteProvider implementation.
2. Daily FNO instruments cache & symbol resolution.
3. Top-of-book depth level 1 bid/ask extraction.
4. Illiquidity filter (spread > 5% or empty depth -> SKIPPED_ILLIQUID).
5. Paper fills (entry at ask, exit at bid).
6. Option ticks table logging (option_ticks table).
7. Price source tracking (REAL_QUOTE vs SYNTHETIC fallback) & separate P&L reporting.
8. WebSocket subscribe & unsubscribe methods.
"""

import os
import sqlite3
import tempfile
import time
import pytest
from unittest.mock import MagicMock, patch

from src.feeds.option_quote_provider import OptionQuoteProvider, IndstocksQuoteProvider
from src.paper.live_paper_tracker import LivePaperTracker


def test_quote_provider_inheritance():
    """Requirement 1: Verify IndstocksQuoteProvider implements OptionQuoteProvider."""
    assert issubclass(IndstocksQuoteProvider, OptionQuoteProvider)
    provider = IndstocksQuoteProvider()
    assert isinstance(provider, OptionQuoteProvider)


def test_daily_instruments_cache_and_resolution():
    """Requirement 2: Verify daily FNO instruments CSV caching and instrument mapping."""
    provider = IndstocksQuoteProvider()
    # Check resolution for NIFTY
    nifty_inst = provider.resolve_instrument("NIFTY", "NEAR", 25400.0, "CE")
    assert nifty_inst is not None
    assert nifty_inst["underlying"] == "NIFTY"
    assert nifty_inst["opt_type"] == "CE"
    assert nifty_inst["scrip_code"].startswith("NFO_")
    assert nifty_inst["ws_instrument"].startswith("NFO:")
    assert nifty_inst["security_id"] != ""

    # Check resolution for SENSEX (BSE)
    sensex_inst = provider.resolve_instrument("SENSEX", "NEAR", 82000.0, "CE")
    assert sensex_inst is not None
    assert sensex_inst["exchange"] == "BSE"
    assert sensex_inst["scrip_code"].startswith("BFO_")
    assert sensex_inst["ws_instrument"].startswith("BFO:")


def test_illiquid_spread_detection():
    """Requirement 9: Verify illiquid signals (> 5% spread or empty depth) are detected."""
    provider = IndstocksQuoteProvider()

    # Mock REST response with 10% spread
    mock_data = {
        "status": "success",
        "data": {
            "NFO_99999": {
                "live_price": 50.0,
                "open_interest": 1000,
                "market_depth": {
                    "NFO_99999": {
                        "depth": [
                            {"buy": {"price": "45.00"}, "sell": {"price": "50.00"}}  # spread = 5/50 = 10%
                        ]
                    }
                }
            }
        }
    }

    with patch.object(provider, "resolve_instrument", return_value={
        "security_id": "99999",
        "scrip_code": "NFO_99999",
        "trading_symbol": "TEST-OPT",
        "underlying": "TEST",
        "exchange": "NSE",
        "expiry": "2026-11-23",
        "strike": 100.0,
        "opt_type": "CE"
    }), patch("requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = mock_data
        mock_get.return_value = mock_resp

        quote = provider.get_quote("TEST", "NEAR", 100.0, "CE")
        assert quote["is_illiquid"] is True
        assert "SPREAD_GT_5%" in quote["illiquid_reason"]


def test_paper_trade_entry_at_ask_and_illiquid_skip():
    """Requirement 6 & 9: Paper fills enter at ask, and illiquid signals are skipped."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        temp_db = f.name

    try:
        tracker = LivePaperTracker(db_path=temp_db)
        mock_provider = MagicMock(spec=IndstocksQuoteProvider)

        # 1. Illiquid quote should be rejected as SKIPPED_ILLIQUID
        mock_provider.get_quote.return_value = {
            "bid": 90.0, "ask": 100.0, "ltp": 95.0, "iv": 15.0, "oi": 5000,
            "ts": time.time(), "token": "12345", "scrip_code": "NFO_12345",
            "symbol": "NIFTY-Nov2026-25400-CE", "is_illiquid": True,
            "illiquid_reason": "SPREAD_GT_5% (10.0%)", "source": "REAL_QUOTE"
        }
        tracker.option_quote_provider = mock_provider

        # Mock market open for equity options
        with patch.object(tracker, "is_market_open_for_sector", return_value=(True, "Open")):
            pos_illiquid = tracker.open_position(
                source_app="TEST",
                sector="INDEX_OPTIONS",
                symbol="NIFTY 25400 CE",
                action="BUY",
                entry_price=25400.0,
                stop_loss=25300.0
            )
            assert pos_illiquid is None  # Must be skipped!

        # 2. Liquid quote should fill at ASK and tag REAL_QUOTE
        mock_provider.get_quote.return_value = {
            "bid": 150.0, "ask": 151.0, "ltp": 150.5, "iv": 14.5, "oi": 100000,
            "ts": time.time(), "token": "64462", "scrip_code": "NFO_64462",
            "symbol": "NIFTY-Nov2026-25400-CE", "is_illiquid": False,
            "illiquid_reason": None, "source": "REAL_QUOTE"
        }

        with patch.object(tracker, "is_market_open_for_sector", return_value=(True, "Open")):
            pos = tracker.open_position(
                source_app="TEST",
                sector="INDEX_OPTIONS",
                symbol="NIFTY 25400 CE",
                action="BUY",
                entry_price=25400.0,
                stop_loss=25300.0
            )
            assert pos is not None
            assert pos["price_source"] == "REAL_QUOTE"
            assert pos["opt_entry_premium"] == 151.0  # Filled at ASK!
            assert pos["opt_token"] == "64462"
            mock_provider.subscribe.assert_called_with("64462", exch="NSE")

    finally:
        try:
            if os.path.exists(temp_db):
                os.remove(temp_db)
        except Exception:
            pass


def test_option_ticks_logging_and_exit_at_bid():
    """Requirement 6 & 7: Test 60s option_ticks logging and exit executed at BID."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        temp_db = f.name

    try:
        tracker = LivePaperTracker(db_path=temp_db)
        mock_provider = MagicMock(spec=IndstocksQuoteProvider)
        mock_provider.get_latest_stream_quote.return_value = None  # fallback to REST
        mock_provider.get_quote.return_value = {
            "bid": 185.0, "ask": 186.0, "ltp": 185.5, "iv": 14.2, "oi": 120000,
            "ts": time.time(), "token": "64462", "scrip_code": "NFO_64462",
            "symbol": "NIFTY-Nov2026-25400-CE", "is_illiquid": False,
            "illiquid_reason": None, "source": "REAL_QUOTE"
        }
        tracker.option_quote_provider = mock_provider

        # Open trade
        with patch.object(tracker, "is_market_open_for_sector", return_value=(True, "Open")), \
             patch.object(tracker.indmoney_feed, "get_quote_with_age", return_value=({"ltp": 25412.70}, 1.0)):
            pos = tracker.open_position(
                source_app="TEST",
                sector="INDEX_OPTIONS",
                symbol="NIFTY 25400 CE",
                action="BUY",
                entry_price=25400.0,
                stop_loss=25300.0
            )
            assert pos is not None
            pos_id = pos["position_id"]

            # Force last tick log time to 70s ago
            pos["_last_tick_log"] = time.time() - 70.0
            pos["_last_rest_poll"] = 0.0

            # Run update_and_trail_positions to simulate a tick
            tracker.update_and_trail_positions()

            # Verify tick was inserted into option_ticks table
            with tracker._get_connection() as conn:
                ticks = conn.execute("SELECT * FROM option_ticks WHERE trade_id = ?", (pos_id,)).fetchall()
                assert len(ticks) >= 1
                row = dict(ticks[0])
                assert row["bid"] == 185.0
                assert row["ask"] == 186.0
                assert row["ltp"] == 185.5
                assert row["oi"] == 120000

            # Close trade and verify exit at BID
            closed = tracker.close_position(pos_id, exit_price=25500.0, exit_reason="TARGET_HIT", force=True)
            assert closed is True
            mock_provider.unsubscribe.assert_called_with("64462", exch="NSE")

            # Check closed ledger
            with tracker._get_connection() as conn:
                closed_trade = conn.execute("SELECT * FROM closed_paper_trades WHERE position_id = ?", (pos_id,)).fetchone()
                assert closed_trade is not None
                assert closed_trade["entry_price"] == 186.0  # entry ask
                assert closed_trade["exit_price"] == 185.0   # exit bid
                assert closed_trade["price_source"] == "REAL_QUOTE"

            # Check P&L report by source
            pnl_report = tracker.get_pnl_by_price_source()
            assert "REAL_QUOTE" in pnl_report
            assert pnl_report["REAL_QUOTE"]["trade_count"] == 1

    finally:
        try:
            if os.path.exists(temp_db):
                os.remove(temp_db)
        except Exception:
            pass
