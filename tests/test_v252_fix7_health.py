"""
Acceptance Test for Item 7: Deep Health Diagnostic Endpoint
- /api/health/deep returns 200 outside market hours, weekends, and holidays with "market_open": false.
- During market hours, returns 503 if feed is stale, scheduler is stalled, or token is expired.
- Checks cached token expiry without calling broker on each ping.
- Returns per-subsystem status in JSON.
"""

import asyncio
import datetime
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from httpx import AsyncClient, ASGITransport

_SCANNER_DIR = Path(__file__).resolve().parent.parent
if str(_SCANNER_DIR) not in sys.path:
    sys.path.insert(0, str(_SCANNER_DIR))

from src.api.server import app, scheduler


@pytest.mark.anyio
async def test_health_deep_outside_market_hours():
    """At 20:00 IST (outside market hours), endpoint must return HTTP 200 with market_open=False."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        with patch("src.utils.indian_market_calendar.is_equity_market_open_ist", return_value=(False, "Off-Hours")):
            resp = await client.get("/api/health/deep")
            assert resp.status_code == 200
            data = resp.json()
            assert data["market_open"] is False
            assert "subsystems" in data
            assert data["subsystems"]["feed"] == "HEALTHY"


@pytest.mark.anyio
async def test_health_deep_stale_feed_during_market_hours():
    """At 11:00 IST during market hours, if feed is stale, endpoint must return HTTP 503."""
    scheduler.is_running = True
    scheduler.is_stale_feed = True  # Simulate stale feed

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        with patch("src.utils.indian_market_calendar.is_equity_market_open_ist", return_value=(True, "Market Open")):
            resp = await client.get("/api/health/deep")
            assert resp.status_code == 503
            data = resp.json()
            assert data["market_open"] is True
            assert data["subsystems"]["feed"] == "STALE_FEED"
            assert any("STALE_FEED" in iss for iss in data["issues"])

    # Reset state
    scheduler.is_stale_feed = False
