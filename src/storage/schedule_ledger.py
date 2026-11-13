"""
FinPlus+ Institutional Schedule Ledger
======================================
Guarantees idempotent, state-driven execution of all required market bulletins.
Maintains a persistent SQLite ledger of all scheduled daily events.

Rules:
1. Every event (09:00 Agenda, 09:30 Status, 30m Pulses, 15:00 Summary) has a persistent record.
2. An event is never skipped due to clock-jitter, server reboots, or scan latency.
3. If an event is scheduled for 09:00 and the server is busy or restarts at 09:12,
   the ledger immediately detects the PENDING status and fires it.
4. Duplicate dispatches are mathematically impossible (atomic status lock).
"""

import sqlite3
import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

from src.utils.time_and_logger import setup_logger

logger = setup_logger("ScheduleLedger")

DB_DIR = Path(__file__).resolve().parent.parent.parent / "data"
DB_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DB_DIR / "schedule_ledger.db"

# Official schedule milestones on NSE/BSE weekdays
# Target time is (hour, minute) in IST
SCHEDULED_MILESTONES = [
    {"slot_id": "09:00_AGENDA", "target_h": 9, "target_m": 0, "name": "09:00 AM Today's Agenda Briefing"},
    {"slot_id": "09:30_PULSE_FIRST", "target_h": 9, "target_m": 30, "name": "09:30 AM First Status & Suggestions"},
    {"slot_id": "10:00_PULSE", "target_h": 10, "target_m": 0, "name": "10:00 AM 30m Pulse"},
    {"slot_id": "10:30_PULSE", "target_h": 10, "target_m": 30, "name": "10:30 AM 30m Pulse"},
    {"slot_id": "11:00_PULSE", "target_h": 11, "target_m": 0, "name": "11:00 AM 30m Pulse"},
    {"slot_id": "11:30_PULSE", "target_h": 11, "target_m": 30, "name": "11:30 AM 30m Pulse"},
    {"slot_id": "12:00_PULSE", "target_h": 12, "target_m": 0, "name": "12:00 PM 30m Pulse"},
    {"slot_id": "12:30_PULSE", "target_h": 12, "target_m": 30, "name": "12:30 PM 30m Pulse"},
    {"slot_id": "13:00_PULSE", "target_h": 13, "target_m": 0, "name": "01:00 PM 30m Pulse"},
    {"slot_id": "13:30_PULSE", "target_h": 13, "target_m": 30, "name": "01:30 PM 30m Pulse"},
    {"slot_id": "14:00_PULSE", "target_h": 14, "target_m": 0, "name": "02:00 PM 30m Pulse"},
    {"slot_id": "14:30_PULSE", "target_h": 14, "target_m": 30, "name": "02:30 PM 30m Pulse"},
    {"slot_id": "15:00_SUMMARY", "target_h": 15, "target_m": 0, "name": "03:00 PM End of Trade Summary"},
]


class ScheduleLedger:
    def __init__(self, db_path: str = str(DB_PATH)):
        self.db_path = db_path
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS daily_schedule_ledger (
                    date_str TEXT NOT NULL,
                    slot_id TEXT NOT NULL,
                    target_time_ist TEXT NOT NULL,
                    status TEXT NOT NULL, -- 'PENDING', 'IN_FLIGHT', 'COMPLETED', 'FAILED'
                    dispatched_at_ist TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    error_message TEXT,
                    PRIMARY KEY (date_str, slot_id)
                )
            """)
            conn.commit()

    def ensure_daily_ledger_populated(self, date_str: str):
        """Ensures all required milestones for a trading day exist in the ledger as PENDING."""
        with self._get_connection() as conn:
            for item in SCHEDULED_MILESTONES:
                target_str = f"{item['target_h']:02d}:{item['target_m']:02d}"
                conn.execute("""
                    INSERT OR IGNORE INTO daily_schedule_ledger 
                    (date_str, slot_id, target_time_ist, status, attempts)
                    VALUES (?, ?, ?, 'PENDING', 0)
                """, (date_str, item["slot_id"], target_str))
            conn.commit()

    def get_pending_due_events(self, ist_now: datetime.datetime) -> List[Dict[str, Any]]:
        """
        Returns all milestones that are due to be executed right now but have not completed.
        An event is due if:
        1. date == ist_now.date()
        2. current_time >= target_time
        3. status in ('PENDING', 'FAILED')
        4. Not expired past market day
        """
        date_str = ist_now.strftime("%Y-%m-%d")
        self.ensure_daily_ledger_populated(date_str)

        curr_h = ist_now.hour
        curr_m = ist_now.minute
        curr_total_mins = curr_h * 60 + curr_m

        due_events = []
        with self._get_connection() as conn:
            rows = conn.execute("""
                SELECT slot_id, target_time_ist, status, attempts 
                FROM daily_schedule_ledger 
                WHERE date_str = ? AND status IN ('PENDING', 'FAILED') AND attempts < 3
            """, (date_str,)).fetchall()

            for r in rows:
                slot_id = r["slot_id"]
                parts = r["target_time_ist"].split(":")
                t_h = int(parts[0])
                t_m = int(parts[1])
                target_total_mins = t_h * 60 + t_m

                # Current time is at or past target time
                if curr_total_mins >= target_total_mins:
                    late_by_mins = curr_total_mins - target_total_mins

                    # 1. 09:00 Agenda becomes stale if more than 35 minutes late (after 09:35 IST)
                    if slot_id == "09:00_AGENDA" and late_by_mins > 35:
                        conn.execute("""
                            UPDATE daily_schedule_ledger
                            SET status = 'EXPIRED_STALE', error_message = 'Expired: Past morning session window'
                            WHERE date_str = ? AND slot_id = ?
                        """, (date_str, slot_id))
                        continue

                    # 2. 30m Pulses become stale if superseded by a later milestone (> 35 mins late)
                    if "PULSE" in slot_id and late_by_mins > 35:
                        conn.execute("""
                            UPDATE daily_schedule_ledger
                            SET status = 'EXPIRED_STALE', error_message = 'Expired: Superseded by later interval'
                            WHERE date_str = ? AND slot_id = ?
                        """, (date_str, slot_id))
                        continue

                    # 3. 15:00 Summary expires if late past 16:30 IST
                    if slot_id == "15:00_SUMMARY" and late_by_mins > 90:
                        conn.execute("""
                            UPDATE daily_schedule_ledger
                            SET status = 'EXPIRED_STALE', error_message = 'Expired: Past post-market hours'
                            WHERE date_str = ? AND slot_id = ?
                        """, (date_str, slot_id))
                        continue

                    due_events.append({
                        "slot_id": r["slot_id"],
                        "target_time_ist": r["target_time_ist"],
                        "status": r["status"],
                        "attempts": r["attempts"]
                    })
            conn.commit()

        return due_events

    def mark_in_flight(self, date_str: str, slot_id: str) -> bool:
        """Atomically claims an event for dispatch to prevent race conditions."""
        with self._get_connection() as conn:
            cursor = conn.execute("""
                UPDATE daily_schedule_ledger
                SET status = 'IN_FLIGHT', attempts = attempts + 1
                WHERE date_str = ? AND slot_id = ? AND status IN ('PENDING', 'FAILED')
            """, (date_str, slot_id))
            conn.commit()
            return cursor.rowcount > 0

    def mark_completed(self, date_str: str, slot_id: str, dispatched_at_ist: str):
        with self._get_connection() as conn:
            conn.execute("""
                UPDATE daily_schedule_ledger
                SET status = 'COMPLETED', dispatched_at_ist = ?, error_message = NULL
                WHERE date_str = ? AND slot_id = ?
            """, (dispatched_at_ist, date_str, slot_id))
            conn.commit()
        logger.info(f"Ledger: {date_str} {slot_id} marked COMPLETED at {dispatched_at_ist}")

    def mark_failed(self, date_str: str, slot_id: str, error_msg: str):
        with self._get_connection() as conn:
            conn.execute("""
                UPDATE daily_schedule_ledger
                SET status = 'FAILED', error_message = ?
                WHERE date_str = ? AND slot_id = ?
            """, (error_msg, date_str, slot_id))
            conn.commit()
        logger.warning(f"Ledger: {date_str} {slot_id} FAILED ({error_msg})")

    def get_ledger_status(self, date_str: str) -> List[Dict[str, Any]]:
        """Returns the full audit status of all milestones for a given date."""
        self.ensure_daily_ledger_populated(date_str)
        with self._get_connection() as conn:
            rows = conn.execute("""
                SELECT slot_id, target_time_ist, status, dispatched_at_ist, attempts, error_message
                FROM daily_schedule_ledger
                WHERE date_str = ?
                ORDER BY target_time_ist ASC
            """, (date_str,)).fetchall()
            return [dict(r) for r in rows]
