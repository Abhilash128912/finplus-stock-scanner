"""
Acceptance Test for Item 4: NTFY Daily Message Persistence & Priority Bypass
- 4a. Persist daily message counter in SQLite (survives restart), cap 220.
- 4b. Stop-loss, exit, and trade-open alerts bypass the cap.
- 4c. Expose remaining quota in /api/health/deep.
"""

import os
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_SCANNER_DIR = Path(__file__).resolve().parent.parent
if str(_SCANNER_DIR) not in sys.path:
    sys.path.insert(0, str(_SCANNER_DIR))

from src.storage.schedule_ledger import ScheduleLedger
from src.notifications.ntfy_dispatcher import NTFYDispatcher, MAX_DAILY_NOTIFICATIONS


def test_ntfy_counter_persists_across_instances(tmp_path):
    """Requirement 4a: Daily counter stored in SQLite survives restarts."""
    db_file = tmp_path / "test_ledger.db"
    from src.utils.time_and_logger import get_ist_now
    today = get_ist_now().strftime("%Y-%m-%d")

    # Instance 1: Initial state
    ledger1 = ScheduleLedger(db_path=str(db_file))
    dispatcher1 = NTFYDispatcher(ledger=ledger1)

    assert dispatcher1.get_sent_count_today() == 0
    assert dispatcher1.get_remaining_quota() == 220

    # Simulate 5 successful dispatches
    with patch("requests.post") as mock_post:
        mock_post.return_value = MagicMock(status_code=200)
        for _ in range(5):
            success = dispatcher1._safe_post("https://ntfy.sh/test", "Test Message", {"Title": "Test"})
            assert success is True

    assert dispatcher1.get_sent_count_today() == 5
    assert dispatcher1.get_remaining_quota() == 215

    # Instance 2 (Simulating server restart / new process reading same SQLite DB)
    ledger2 = ScheduleLedger(db_path=str(db_file))
    dispatcher2 = NTFYDispatcher(ledger=ledger2)

    # Counter must be unchanged after restart
    assert dispatcher2.get_sent_count_today() == 5
    assert dispatcher2.get_remaining_quota() == 215


def test_priority_alerts_bypass_cap(tmp_path):
    """Requirement 4b: Stop-loss, exit, and trade-open alerts bypass the 220 cap."""
    db_file = tmp_path / "test_ledger_cap.db"
    from src.utils.time_and_logger import get_ist_now
    today = get_ist_now().strftime("%Y-%m-%d")

    ledger = ScheduleLedger(db_path=str(db_file))
    dispatcher = NTFYDispatcher(ledger=ledger)

    # Fast forward counter to 220 (Cap hit)
    with ledger._get_connection() as conn:
        conn.execute("""
            INSERT INTO ntfy_daily_quota (date_str, messages_sent, updated_at)
            VALUES (?, 220, ?)
        """, (today, "2026-11-13T10:00:00"))
        conn.commit()

    assert dispatcher.get_sent_count_today() == 220
    assert dispatcher.get_remaining_quota() == 0

    # Routine pulse without priority must be BLOCKED
    with patch("requests.post") as mock_post:
        res_routine = dispatcher._safe_post(
            "https://ntfy.sh/test", "Routine Pulse", {"Title": "Pulse"}, is_priority=False
        )
        assert res_routine is False
        assert mock_post.call_count == 0

    # Priority alert (Stop loss / trade exit / trade open) MUST BYPASS
    with patch("requests.post") as mock_post:
        mock_post.return_value = MagicMock(status_code=200)
        res_priority = dispatcher._safe_post(
            "https://ntfy.sh/test", "Stop Loss Hit", {"Title": "Stop Loss"}, is_priority=True
        )
        assert res_priority is True
        assert mock_post.call_count == 1
        assert dispatcher.get_sent_count_today() == 221

