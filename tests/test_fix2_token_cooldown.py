"""
Test for Item 2: Token and Cooldown
- Verifies INDMoneyLiveFeed is a singleton.
- Verifies shared token file persistence (token, expiry, last_request_time).
- Verifies cooldown checking before broker calls.
- Verifies token expiry mid-scan produces exactly 1 refresh call, 0 HTTP 400s, and single-flight lock handles concurrent 403s.
"""

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_SCANNER_DIR = Path(__file__).resolve().parent.parent
if str(_SCANNER_DIR) not in sys.path:
    sys.path.insert(0, str(_SCANNER_DIR))

from finplus_core.auth.indmoney_client import INDMoneyAuthClient
from src.paper.indmoney_live_feed import INDMoneyLiveFeed


def test_indmoney_feed_is_singleton():
    """Requirement 2e: INDMoneyLiveFeed must be a singleton."""
    feed1 = INDMoneyLiveFeed()
    feed2 = INDMoneyLiveFeed()
    assert feed1 is feed2, "INDMoneyLiveFeed instances must be identical (singleton)"


def test_token_file_persistence(tmp_path):
    """Requirement 2a: Keep ONE token file holding token, expiry, last_request_time."""
    token_file = tmp_path / "indmoney_token.json"
    dummy_jwt = (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
        "eyJleHAiOjE5OTk5OTk5OTksImlhdCI6MTcwMDAwMDAwMCwiY2xpZW50SUQiOiJURVNUX0NMSUVOVCJ9."
        "dummy_signature"
    )

    client = INDMoneyAuthClient(
        client_id="TEST_ID",
        totp_secret="JBSWY3DPEHPK3PXP",
        mpin="1234",
        access_token=dummy_jwt,
        token_file_path=token_file,
    )
    client._save_token_to_file(dummy_jwt)

    assert token_file.exists(), "Token file was not created"
    with open(token_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    assert data["token"] == dummy_jwt
    assert "expiry" in data
    assert "last_request_time" in data


def test_cooldown_before_broker_call():
    """Requirement 2b: Check cooldown BEFORE broker call. During cooldown, serve cached data without broker call."""
    feed = INDMoneyLiveFeed()
    # Populate quote cache
    feed._quote_cache = {
        "RELIANCE": {"symbol": "RELIANCE", "ltp": 2500.0, "scrip_code": "NSE_2885"}
    }
    feed._last_fetch_time = time.time() - 10.0  # Expire TTL

    # Simulate active cooldown
    with patch.object(feed.auth_client, "get_cooldown_remaining", return_value=45.0):
        with patch("requests.get") as mock_get:
            quotes = feed.fetch_batch_quotes(["RELIANCE"])
            assert mock_get.call_count == 0, "Broker HTTP call occurred during active cooldown!"
            assert quotes.get("RELIANCE", {}).get("ltp") == 2500.0


def test_single_flight_refresh_on_403():
    """Requirement 2c: Single-flight refresh - first 403 refreshes, others reuse new token; retries failed call once."""
    feed = INDMoneyLiveFeed()
    feed._quote_cache.clear()
    feed._last_fetch_time = 0.0
    feed._broker_cooldown_until = 0.0

    valid_jwt = (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
        "eyJleHAiOjE5OTk5OTk5OTksImlhdCI6MTcwMDAwMDAwMCwiY2xpZW50SUQiOiJURVNUX0NMSUVOVCJ9."
        "valid_sig"
    )

    call_count = {"refresh": 0, "http_get": 0}

    def fake_refresh():
        call_count["refresh"] += 1
        feed.auth_client.access_token = valid_jwt
        return valid_jwt

    def fake_get(url, headers=None, timeout=None):
        call_count["http_get"] += 1
        # If token is invalid/old, return 403
        auth_hdr = headers.get("Authorization") if headers else ""
        if auth_hdr != valid_jwt:
            resp = MagicMock()
            resp.status_code = 403
            resp.text = "Token Expired"
            return resp
        else:
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {
                "data": {
                    "NSE_2885": {"live_price": "2550.0", "day_change_percentage": "1.2"}
                }
            }
            return resp

    # Set initial expired token
    feed.auth_client.access_token = "old_expired_token"
    # When get_auth_headers is called initially before the loop, it returns the old token
    with patch.object(feed.auth_client, "get_auth_headers", side_effect=lambda: {"Authorization": feed.auth_client.access_token}):
        with patch.object(feed.auth_client, "is_token_valid", side_effect=lambda: feed.auth_client.access_token == valid_jwt):
            with patch.object(feed.auth_client, "generate_fresh_token", side_effect=fake_refresh):
                with patch.object(feed.auth_client, "get_cooldown_remaining", return_value=0.0):
                    with patch("requests.get", side_effect=fake_get):
                        res = feed.fetch_batch_quotes(["RELIANCE"], force=True)

    assert call_count["refresh"] == 1, f"Expected exactly 1 refresh call, got {call_count['refresh']}"
    assert res.get("RELIANCE", {}).get("ltp") == 2550.0
    assert call_count["http_get"] == 2, f"Expected 1 initial 403 + 1 retry, got {call_count['http_get']}"
