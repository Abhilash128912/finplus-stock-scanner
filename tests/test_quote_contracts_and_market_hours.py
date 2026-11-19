import pytest
import time
from unittest.mock import MagicMock, patch

from src.paper.indmoney_live_feed import INDMoneyLiveFeed


def test_stale_exchange_timestamp_marks_stale(monkeypatch):
    """
    Requirement: If exchange timestamp is older than 15s during market hours,
    mark STALE and return ltp=None.
    """
    feed = INDMoneyLiveFeed()
    feed.auth_client = MagicMock()
    feed.auth_client.get_auth_headers.return_value = {"Authorization": "Bearer test"}
    feed.auth_client.get_cooldown_remaining.return_value = 0.0

    # Mock market hours as OPEN
    monkeypatch.setattr(
        "src.utils.indian_market_calendar.is_equity_market_open_ist",
        lambda: (True, "Open")
    )

    scrip = feed.resolve_scrip_code("RELIANCE")

    class MockResp:
        status_code = 200
        headers = {}
        def json(self):
            # Exchange timestamp 30s ago
            stale_ts = time.time() - 30.0
            return {
                "status": "success",
                "data": {
                    scrip: {
                        "live_price": 2500.0,
                        "day_change_percentage": 0.5,
                        "timestamp": stale_ts
                    }
                }
            }

    monkeypatch.setattr("requests.get", lambda *args, **kwargs: MockResp())

    results = feed.fetch_batch_quotes(["RELIANCE"])
    rel = results.get("RELIANCE")
    assert rel is not None
    assert rel["feed_status"] == "STALE"
    assert rel["ltp"] is None
    assert rel["tick_age"] >= 29.0


def test_error_returns_none_with_no_cached_value(monkeypatch):
    """
    Requirement: Never return a cached price on error. On any failure return None
    and set last_error.
    """
    feed = INDMoneyLiveFeed()
    feed.auth_client = MagicMock()
    feed.auth_client.get_auth_headers.return_value = {"Authorization": "Bearer test"}
    feed.auth_client.get_cooldown_remaining.return_value = 0.0

    # Seed previous quote cache
    feed._quote_cache["RELIANCE"] = {"ltp": 2500.0, "feed_status": "OK"}

    # Mock market hours as OPEN
    monkeypatch.setattr(
        "src.utils.indian_market_calendar.is_equity_market_open_ist",
        lambda: (True, "Open")
    )

    class MockErrResp:
        status_code = 500
        headers = {}
        text = "Internal Server Error"
        def json(self):
            return {}

    monkeypatch.setattr("requests.get", lambda *args, **kwargs: MockErrResp())

    results = feed.fetch_batch_quotes(["RELIANCE"])
    rel = results.get("RELIANCE")
    assert rel is not None
    assert rel["ltp"] is None
    assert rel["feed_status"] == "ERROR"
    assert feed.last_errors["RELIANCE"] is not None


def test_outside_market_hours_marks_market_closed(monkeypatch):
    """
    Requirement: Outside market hours, mark MARKET_CLOSED, not STALE.
    """
    feed = INDMoneyLiveFeed()
    feed.auth_client = MagicMock()
    feed.auth_client.get_auth_headers.return_value = {"Authorization": "Bearer test"}
    feed.auth_client.get_cooldown_remaining.return_value = 0.0

    # Mock market hours as CLOSED
    monkeypatch.setattr(
        "src.utils.indian_market_calendar.is_equity_market_open_ist",
        lambda: (False, "Closed")
    )

    scrip = feed.resolve_scrip_code("RELIANCE")

    class MockOkResp:
        status_code = 200
        headers = {}
        def json(self):
            # Old timestamp from previous day
            return {
                "status": "success",
                "data": {
                    scrip: {
                        "live_price": 2500.0,
                        "day_change_percentage": 0.5,
                        "timestamp": time.time() - 3600.0
                    }
                }
            }

    monkeypatch.setattr("requests.get", lambda *args, **kwargs: MockOkResp())

    results = feed.fetch_batch_quotes(["RELIANCE"])
    rel = results.get("RELIANCE")
    assert rel is not None
    assert rel["feed_status"] == "MARKET_CLOSED"
    assert rel["ltp"] == 2500.0


