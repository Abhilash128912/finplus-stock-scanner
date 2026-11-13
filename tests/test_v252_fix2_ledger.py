"""
Acceptance Test for Item 2: Schedule Ledger
- UNIQUE key (date_str, slot_id) with INSERT OR IGNORE.
- On startup, resets IN_FLIGHT rows older than 2 minutes to PENDING (counts as attempt, max 3 attempts).
- Never send/mark a milestone whose row is COMPLETED.
- Kill app mid-send, restart -> milestone is sent exactly once.
- Double trigger -> exactly one row in DB.
"""

import datetime
import sys
import time
from pathlib import Path

import pytest

_SCANNER_DIR = Path(__file__).resolve().parent.parent
if str(_SCANNER_DIR) not in sys.path:
    sys.path.insert(0, str(_SCANNER_DIR))

from src.storage.schedule_ledger import ScheduleLedger


def test_unique_constraint_and_double_trigger(tmp_path):
    db_file = tmp_path / "test_unique_ledger.db"
    ledger = ScheduleLedger(db_path=str(db_file))

    date_str = "2026-11-13"
    ledger.ensure_daily_ledger_populated(date_str)

    # Calling ensure_daily_ledger_populated again (double trigger) must not create duplicate rows
    ledger.ensure_daily_ledger_populated(date_str)

    with ledger._get_connection() as conn:
        rows = conn.execute(
            "SELECT COUNT(*) as cnt FROM daily_schedule_ledger WHERE date_str = ? AND slot_id = '09:00_AGENDA'",
            (date_str,)
        ).fetchone()
        assert rows["cnt"] == 1, "Duplicate milestone row was created!"


def test_in_flight_recovery_on_startup(tmp_path):
    db_file = tmp_path / "test_recovery_ledger.db"
    ledger = ScheduleLedger(db_path=str(db_file))

    date_str = "2026-11-13"
    slot_id = "09:00_AGENDA"
    ledger.ensure_daily_ledger_populated(date_str)

    # Mark IN_FLIGHT with timestamp 3 minutes ago (simulating app killed mid-send)
    three_mins_ago = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=3)).isoformat()
    with ledger._get_connection() as conn:
        conn.execute("""
            UPDATE daily_schedule_ledger
            SET status = 'IN_FLIGHT', attempts = 1, updated_at_utc = ?
            WHERE date_str = ? AND slot_id = ?
        """, (three_mins_ago, date_str, slot_id))
        conn.commit()

    # Re-instantiate ledger (simulating server restart)
    ledger_restarted = ScheduleLedger(db_path=str(db_file))

    # Row must be recovered to PENDING
    with ledger_restarted._get_connection() as conn:
        row = conn.execute("""
            SELECT status, attempts FROM daily_schedule_ledger
            WHERE date_str = ? AND slot_id = ?
        """, (date_str, slot_id)).fetchone()
        assert row["status"] == "PENDING", f"Expected PENDING after restart, got {row['status']}"


def test_never_send_completed_milestone(tmp_path):
    db_file = tmp_path / "test_completed_ledger.db"
    ledger = ScheduleLedger(db_path=str(db_file))

    date_str = "2026-11-13"
    slot_id = "09:00_AGENDA"
    ledger.ensure_daily_ledger_populated(date_str)

    # Claim and complete milestone
    claimed = ledger.mark_in_flight(date_str, slot_id)
    assert claimed is True
    ledger.mark_completed(date_str, slot_id, "09:00:05 AM IST")

    # Second claim must fail
    claimed_again = ledger.mark_in_flight(date_str, slot_id)
    assert claimed_again is False, "A COMPLETED milestone was claimed again!"

    # Must not appear in get_pending_due_events
    ist_sim = datetime.datetime(2026, 11, 13, 9, 5, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=5, minutes=30)))
    pending = ledger.get_pending_due_events(ist_sim)
    agenda_pending = [p for p in pending if p["slot_id"] == "09:00_AGENDA"]
    assert len(agenda_pending) == 0, "COMPLETED milestone appeared in pending list"

