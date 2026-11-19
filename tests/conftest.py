import os
import sys
from pathlib import Path
import pytest
from unittest.mock import patch

# Ensure root of finplus_stock_scanner is in sys.path
_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# Set test environment globally before any test modules import or execute
os.environ["FINPLUS_ENVIRONMENT"] = "test"

@pytest.fixture(autouse=True)
def guard_against_external_leaks(monkeypatch):
    """
    Global safety fixture ensuring that under NO circumstances can test runs
    leak synthetic trades or notifications to external production sinks
    (Google Sheets Webhook, Live NTFY channels, etc.).
    """
    monkeypatch.setenv("FINPLUS_ENVIRONMENT", "test")
    # Suppress outgoing Google Sheet calls across all tests
    monkeypatch.setattr(
        "src.paper.live_paper_tracker.LivePaperTracker._push_trade_to_google_sheet",
        lambda self, record: None
    )
    yield
