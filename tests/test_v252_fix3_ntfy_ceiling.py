"""
Acceptance Test for Item 3: NTFY Hard Ceiling & Tiered Quotas
- Routine alerts stop at 220.
- Trade open and close alerts may use up to 240.
- Only stop-loss alerts may use 241-248.
- At 248, block everything and log WARNING once.
- Counter resets by IST date.
"""

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_SCANNER_DIR = Path(__file__).resolve().parent.parent
if str(_SCANNER_DIR) not in sys.path:
    sys.path.insert(0, str(_SCANNER_DIR))

from src.storage.schedule_ledger import ScheduleLedger
from src.notifications.ntfy_dispatcher import NTFYDispatcher
from src.utils.time_and_logger import get_ist_now


def test_tiered_quota_enforcement(tmp_path):
    db_file = tmp_path / "test_tiered_quota.db"
    ledger = ScheduleLedger(db_path=str(db_file))
    dispatcher = NTFYDispatcher(ledger=ledger)
    today = get_ist_now().strftime("%Y-%m-%d")

    def set_quota_count(cnt: int):
        with ledger._get_connection() as conn:
            conn.execute("""
                INSERT INTO ntfy_daily_quota (date_str, messages_sent, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(date_str) DO UPDATE SET messages_sent = excluded.messages_sent
            """, (today, cnt, "2026-11-13T10:00:00"))
            conn.commit()

    with patch("requests.post") as mock_post:
        mock_post.return_value = MagicMock(status_code=200)

        # 1. At count = 219: ALL alerts allowed
        set_quota_count(219)
        assert dispatcher._safe_post("https://ntfy.sh/test", "msg", {"Title": "T"}, alert_type="ROUTINE") is True
        assert dispatcher._safe_post("https://ntfy.sh/test", "msg", {"Title": "T"}, alert_type="TRADE_OPEN") is True
        assert dispatcher._safe_post("https://ntfy.sh/test", "msg", {"Title": "T"}, alert_type="STOP_LOSS") is True

        # 2. At count = 225: Routine blocked, Trade actions allowed, Stop loss allowed
        set_quota_count(225)
        assert dispatcher._safe_post("https://ntfy.sh/test", "msg", {"Title": "T"}, alert_type="ROUTINE") is False
        assert dispatcher._safe_post("https://ntfy.sh/test", "msg", {"Title": "T"}, alert_type="TRADE_OPEN") is True
        assert dispatcher._safe_post("https://ntfy.sh/test", "msg", {"Title": "T"}, alert_type="TRADE_CLOSE") is True
        assert dispatcher._safe_post("https://ntfy.sh/test", "msg", {"Title": "T"}, alert_type="STOP_LOSS") is True

        # 3. At count = 242: Routine blocked, Trade actions blocked, ONLY Stop loss allowed
        set_quota_count(242)
        assert dispatcher._safe_post("https://ntfy.sh/test", "msg", {"Title": "T"}, alert_type="ROUTINE") is False
        assert dispatcher._safe_post("https://ntfy.sh/test", "msg", {"Title": "T"}, alert_type="TRADE_OPEN") is False
        assert dispatcher._safe_post("https://ntfy.sh/test", "msg", {"Title": "T"}, alert_type="TRADE_CLOSE") is False
        assert dispatcher._safe_post("https://ntfy.sh/test", "msg", {"Title": "T"}, alert_type="STOP_LOSS") is True

        # 4. At count = 249: EVERYTHING blocked (Hard ceiling reached)
        set_quota_count(249)
        assert dispatcher._safe_post("https://ntfy.sh/test", "msg", {"Title": "T"}, alert_type="ROUTINE") is False
        assert dispatcher._safe_post("https://ntfy.sh/test", "msg", {"Title": "T"}, alert_type="TRADE_OPEN") is False
        assert dispatcher._safe_post("https://ntfy.sh/test", "msg", {"Title": "T"}, alert_type="TRADE_CLOSE") is False
        assert dispatcher._safe_post("https://ntfy.sh/test", "msg", {"Title": "T"}, alert_type="STOP_LOSS") is False

