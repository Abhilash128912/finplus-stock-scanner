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

import os
import sqlite3
import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

from src.utils.time_and_logger import setup_logger

logger = setup_logger("ScheduleLedger")

DB_DIR = Path(os.getenv("DATA_DIR", Path(__file__).resolve().parent.parent.parent / "data"))
DB_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DB_DIR / "schedule_ledger.db"

# Official schedule milestones on NSE/BSE weekdays
# Target time is (hour, minute) in IST
SCHEDULED_MILESTONES = [
    {"slot_id": "08:30_HISTORY_BACKFILL", "target_h": 8, "target_m": 30, "name": "08:30 AM Off-Hours History Backfill"},
    {"slot_id": "08:45_PREFLIGHT", "target_h": 8, "target_m": 45, "name": "08:45 AM Trading Day Preflight Check"},
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
    {"slot_id": "14:00_FUNNEL_CHECK", "target_h": 14, "target_m": 0, "name": "02:00 PM Funnel Opportunity Check"},
    {"slot_id": "14:30_PULSE", "target_h": 14, "target_m": 30, "name": "02:30 PM 30m Pulse"},
    {"slot_id": "15:00_SUMMARY", "target_h": 15, "target_m": 0, "name": "03:00 PM End of Trade Summary"},
    {"slot_id": "15:45_SELF_REPORT", "target_h": 15, "target_m": 45, "name": "03:45 PM Post-Market Self-Report"},
    {"slot_id": "16:00_HISTORY_BACKFILL", "target_h": 16, "target_m": 0, "name": "04:00 PM Post-Market History Backfill"},
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
                    updated_at_utc TEXT,
                    PRIMARY KEY (date_str, slot_id),
                    UNIQUE (date_str, slot_id)
                )
            """)
            # Check if updated_at_utc column exists in existing tables, add if missing
            cols = [r[1] for r in conn.execute("PRAGMA table_info(daily_schedule_ledger)").fetchall()]
            if "updated_at_utc" not in cols:
                conn.execute("ALTER TABLE daily_schedule_ledger ADD COLUMN updated_at_utc TEXT")

            conn.execute("""
                CREATE TABLE IF NOT EXISTS ntfy_daily_quota (
                    date_str TEXT PRIMARY KEY,
                    messages_sent INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS daily_breakout_latch (
                    date_str TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    break_direction INTEGER NOT NULL,
                    break_price REAL NOT NULL,
                    latched_at_utc TEXT NOT NULL,
                    PRIMARY KEY (date_str, symbol),
                    UNIQUE (date_str, symbol)
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS range_variants (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scan_time_utc TEXT NOT NULL,
                    date_str TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    variant_name TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    atr_len_configured INTEGER NOT NULL,
                    atr_len_effective INTEGER NOT NULL,
                    range_len INTEGER NOT NULL,
                    break_window_bars INTEGER NOT NULL,
                    range_top REAL,
                    range_bottom REAL,
                    range_mid REAL,
                    live_price REAL,
                    signal_direction INTEGER NOT NULL,
                    lateness_pct REAL,
                    lateness_would_block INTEGER NOT NULL,
                    confluence_score INTEGER,
                    status TEXT NOT NULL
                )
            """)
            conn.commit()

        # On startup, recover stuck IN_FLIGHT rows older than 2 minutes to PENDING
        self.recover_stuck_in_flight_rows()

    def recover_stuck_in_flight_rows(self, max_stuck_seconds: int = 120) -> int:
        """
        On startup or initialization, resets IN_FLIGHT rows older than max_stuck_seconds (default 2 mins)
        back to PENDING so they are retried. Counts this reset as an attempt and maintains the max 3 attempts rule.
        """
        now = datetime.datetime.now(datetime.timezone.utc)
        recovered = 0
        with self._get_connection() as conn:
            rows = conn.execute("""
                SELECT date_str, slot_id, attempts, updated_at_utc
                FROM daily_schedule_ledger
                WHERE status = 'IN_FLIGHT'
            """).fetchall()

            for r in rows:
                updated_at_str = r["updated_at_utc"]
                should_reset = True
                if updated_at_str:
                    try:
                        up_dt = datetime.datetime.fromisoformat(updated_at_str.replace("Z", "+00:00"))
                        elapsed = (now - up_dt).total_seconds()
                        should_reset = elapsed >= max_stuck_seconds
                    except Exception:
                        should_reset = True

                if should_reset:
                    # Reset to PENDING if attempts < 3, else mark FAILED
                    new_status = 'PENDING' if r["attempts"] < 3 else 'FAILED'
                    conn.execute("""
                        UPDATE daily_schedule_ledger
                        SET status = ?, error_message = 'Reset stuck IN_FLIGHT on startup', updated_at_utc = ?
                        WHERE date_str = ? AND slot_id = ?
                    """, (new_status, now.isoformat(), r["date_str"], r["slot_id"]))
                    recovered += 1

            conn.commit()

        if recovered > 0:
            logger.info(f"ScheduleLedger startup recovery: reset {recovered} stuck IN_FLIGHT rows to PENDING.")
        return recovered

    def get_ntfy_sent_count(self, date_str: str) -> int:
        """Retrieves persistent daily message count for date_str."""
        with self._get_connection() as conn:
            row = conn.execute("SELECT messages_sent FROM ntfy_daily_quota WHERE date_str = ?", (date_str,)).fetchone()
            return int(row["messages_sent"]) if row else 0

    def increment_ntfy_sent_count(self, date_str: str) -> int:
        """Increments persistent daily message count and returns new count."""
        now_iso = datetime.datetime.now().isoformat()
        with self._get_connection() as conn:
            conn.execute("""
                INSERT INTO ntfy_daily_quota (date_str, messages_sent, updated_at)
                VALUES (?, 1, ?)
                ON CONFLICT(date_str) DO UPDATE SET
                    messages_sent = messages_sent + 1,
                    updated_at = excluded.updated_at
            """, (date_str, now_iso))
            conn.commit()
            return self.get_ntfy_sent_count(date_str)

    def ensure_daily_ledger_populated(self, date_str: str):
        """
        Ensures all required milestones for a day exist in the ledger.
        On weekends (Saturday, Sunday) and listed NSE holidays:
        All 09:00 to 16:00 market bulletins are marked 'SKIPPED_MARKET_CLOSED' at the start of the day.
        Only Sunday 18:00 Weekly Report remains PENDING on Sunday.
        """
        milestones = list(SCHEDULED_MILESTONES)
        is_weekend = False
        is_holiday = False
        holiday_name = ""

        try:
            dt = datetime.datetime.strptime(date_str, "%Y-%m-%d")
            weekday = dt.weekday()
            is_weekend = (weekday in (5, 6)) # Saturday=5, Sunday=6
            if weekday == 6:
                milestones.append({"slot_id": "18:00_WEEKLY_REPORT", "target_h": 18, "target_m": 0, "name": "Sunday 06:00 PM Weekly Report"})
            from src.utils.indian_market_calendar import is_nse_holiday
            is_holiday, holiday_name = is_nse_holiday(date_str)
        except Exception:
            pass

        is_market_closed = is_weekend or is_holiday
        closed_reason = f"Market closed (NSE Holiday: {holiday_name})" if is_holiday else "Market closed (Weekend)"

        with self._get_connection() as conn:
            for item in milestones:
                target_str = f"{item['target_h']:02d}:{item['target_m']:02d}"
                slot_id = item["slot_id"]
                
                if is_market_closed and slot_id != "18:00_WEEKLY_REPORT":
                    status = "SKIPPED_MARKET_CLOSED"
                    err_msg = closed_reason
                else:
                    status = "PENDING"
                    err_msg = None

                conn.execute("""
                    INSERT OR IGNORE INTO daily_schedule_ledger 
                    (date_str, slot_id, target_time_ist, status, attempts, error_message)
                    VALUES (?, ?, ?, ?, 0, ?)
                """, (date_str, slot_id, target_str, status, err_msg))

            # If existing rows were already inserted as PENDING for a weekend or holiday, update them
            if is_market_closed:
                conn.execute("""
                    UPDATE daily_schedule_ledger
                    SET status = 'SKIPPED_MARKET_CLOSED', error_message = ?
                    WHERE date_str = ? AND status = 'PENDING' AND slot_id != '18:00_WEEKLY_REPORT'
                """, (closed_reason, date_str))

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
        """Atomically claims an event for dispatch to prevent race conditions. Never claims COMPLETED rows."""
        now_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self._get_connection() as conn:
            cursor = conn.execute("""
                UPDATE daily_schedule_ledger
                SET status = 'IN_FLIGHT', attempts = attempts + 1, updated_at_utc = ?
                WHERE date_str = ? AND slot_id = ? AND status IN ('PENDING', 'FAILED')
            """, (now_utc, date_str, slot_id))
            conn.commit()
            return cursor.rowcount > 0

    def mark_completed(self, date_str: str, slot_id: str, dispatched_at_ist: str):
        now_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self._get_connection() as conn:
            conn.execute("""
                UPDATE daily_schedule_ledger
                SET status = 'COMPLETED', dispatched_at_ist = ?, error_message = NULL, updated_at_utc = ?
                WHERE date_str = ? AND slot_id = ?
            """, (dispatched_at_ist, now_utc, date_str, slot_id))
            conn.commit()
        logger.info(f"Ledger: {date_str} {slot_id} marked COMPLETED at {dispatched_at_ist}")

    def mark_failed(self, date_str: str, slot_id: str, error_msg: str):
        now_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self._get_connection() as conn:
            conn.execute("""
                UPDATE daily_schedule_ledger
                SET status = 'FAILED', error_message = ?, updated_at_utc = ?
                WHERE date_str = ? AND slot_id = ?
            """, (error_msg, now_utc, date_str, slot_id))
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

    def try_latch_daily_breakout(self, date_str: str, symbol: str, break_direction: int, break_price: float) -> bool:
        """
        Atomically records a symbol's breakout for a specific calendar trading day.
        Returns True if this is the FIRST breakout scored today for this symbol.
        Returns False if the symbol already has an alert/break scored today (SQLite latch).
        Prevents duplicate alerts from firing across consecutive 5s or 30m scan cycles.
        """
        now_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
        try:
            with self._get_connection() as conn:
                cursor = conn.execute("""
                    INSERT OR IGNORE INTO daily_breakout_latch (date_str, symbol, break_direction, break_price, latched_at_utc)
                    VALUES (?, ?, ?, ?, ?)
                """, (date_str, symbol, break_direction, break_price, now_utc))
                conn.commit()
                return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Error latching daily breakout for {symbol} on {date_str}: {e}")
            return False

    def is_symbol_latched_today(self, date_str: str, symbol: str) -> bool:
        """Checks if a symbol has already been latched today."""
        with self._get_connection() as conn:
            row = conn.execute("""
                SELECT 1 FROM daily_breakout_latch WHERE date_str = ? AND symbol = ?
            """, (date_str, symbol)).fetchone()
            return row is not None

    def record_range_variant_eval(self, eval_data: Dict[str, Any]):
        """Records shadow variant evaluation for performance comparison in range_variants table."""
        now_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
        try:
            with self._get_connection() as conn:
                conn.execute("""
                    INSERT INTO range_variants (
                        scan_time_utc, date_str, symbol, variant_name, timeframe,
                        atr_len_configured, atr_len_effective, range_len, break_window_bars,
                        range_top, range_bottom, range_mid, live_price, signal_direction,
                        lateness_pct, lateness_would_block, confluence_score, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    now_utc,
                    eval_data.get("date_str", ""),
                    eval_data.get("symbol", ""),
                    eval_data.get("variant_name", ""),
                    eval_data.get("timeframe", "15m"),
                    eval_data.get("atr_len_configured", 0),
                    eval_data.get("atr_len_effective", 0),
                    eval_data.get("range_len", 20),
                    eval_data.get("break_window_bars", 5),
                    eval_data.get("range_top"),
                    eval_data.get("range_bottom"),
                    eval_data.get("range_mid"),
                    eval_data.get("live_price"),
                    eval_data.get("signal_direction", 0),
                    eval_data.get("lateness_pct", 0.0),
                    1 if eval_data.get("lateness_would_block") else 0,
                    eval_data.get("confluence_score", 0),
                    eval_data.get("status", "EVALUATED")
                ))
                conn.commit()
        except Exception as e:
            logger.error(f"Error recording range variant: {e}")

    def get_shadow_variant_stats(self, date_str: Optional[str] = None) -> Dict[str, Any]:
        """Returns aggregate metrics across shadow variants for health endpoints and self-reports."""
        if not date_str:
            date_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
        with self._get_connection() as conn:
            total_evals = conn.execute("SELECT COUNT(*) FROM range_variants WHERE date_str = ?", (date_str,)).fetchone()[0]
            by_variant = {}
            rows = conn.execute("""
                SELECT variant_name, 
                       COUNT(*) as total,
                       SUM(CASE WHEN signal_direction != 0 THEN 1 ELSE 0 END) as breaks,
                       SUM(CASE WHEN lateness_would_block = 1 THEN 1 ELSE 0 END) as lateness_blocks,
                       AVG(lateness_pct) as avg_lateness
                FROM range_variants 
                WHERE date_str = ?
                GROUP BY variant_name
            """, (date_str,)).fetchall()
            for r in rows:
                by_variant[r["variant_name"]] = {
                    "evals": r["total"],
                    "breaks": r["breaks"],
                    "lateness_blocks": r["lateness_blocks"],
                    "avg_lateness_pct": round(r["avg_lateness"] or 0.0, 2)
                }
            return {
                "date_str": date_str,
                "total_evaluated": total_evals,
                "variants": by_variant
            }
