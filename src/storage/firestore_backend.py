"""
FinPlus+ Firestore Backend (Phase 2B Firebase Persistence)
=========================================================
Mirrors durable state (paper trades, schedule ledger, NTFY quota, daily latch,
and compact candle history) to Google Cloud Firestore (Firebase Free Spark Plan).
- Authority: Local SQLite is always the fast, authoritative local store.
- Free Spark Quota Limits: 50,000 reads/day, 20,000 writes/day, 20,000 deletes/day, 1 GiB storage.
- Quota Guard:
    * 80% (16,000 writes, 40,000 reads): Stop non-essential writes (candles, stats).
    * 95% (19,000 writes, 47,500 reads): Only critical paper trades permitted.
- Credentials: Read ONLY from env var FIREBASE_CREDENTIALS_JSON (never logged or saved to disk).
- Isolation: Collection prefix 'env/live/' vs 'env/test/' prevents laptop tests from touching production.
- Candle History: One compact gzipped base64 document per symbol (never 1 doc per bar).
"""

import os
import json
import gzip
import base64
import time
import datetime
import threading
from typing import Dict, Any, List, Optional, Tuple, Set
from pathlib import Path

from src.utils.time_and_logger import setup_logger, get_utc_iso

logger = setup_logger("FirestoreBackend")

# Free Spark Plan Daily Ceilings
MAX_WRITES_PER_DAY = 20000
MAX_READS_PER_DAY = 50000
MAX_DELETES_PER_DAY = 20000

WARN_WRITES_80 = int(MAX_WRITES_PER_DAY * 0.80) # 16,000
WARN_WRITES_95 = int(MAX_WRITES_PER_DAY * 0.95) # 19,000
WARN_READS_80 = int(MAX_READS_PER_DAY * 0.80)   # 40,000
WARN_READS_95 = int(MAX_READS_PER_DAY * 0.95)   # 47,500


class FirestoreBackend:
    _instance: Optional["FirestoreBackend"] = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    @classmethod
    def reset_for_tests(cls):
        cls._instance = None

    def __init__(self, env_prefix: Optional[str] = None, credentials_json: Optional[str] = None):
        if env_prefix:
            self.env_prefix = env_prefix.strip().lower()
        if getattr(self, "_initialized", False):
            return

        self.env_prefix = env_prefix or os.getenv("FIREBASE_ENV_PREFIX", "live").strip().lower()
        self.credentials_json_str = credentials_json or os.getenv("FIREBASE_CREDENTIALS_JSON", "")
        self.client = None
        self.is_connected = False
        self.mode = "LOCAL_ONLY"
        self._lock = threading.Lock()

        # Daily Quota Counters (IST Date)
        self.current_ist_date = ""
        self.writes_today = 0
        self.reads_today = 0
        self.deletes_today = 0
        self.alerted_80 = False
        self.alerted_95 = False

        # Batch write flush pacing (max 1 flush per 5s)
        self.last_flush_time = 0.0
        self.pending_writes: Dict[str, Dict[str, Any]] = {} # doc_path -> data

        # Restore and Sync Telemetry
        self.last_restore_result: Dict[str, Any] = {}
        self.last_restore_reason: str = "NO_RESTORE_RUN"
        self.last_candle_sync_time: Optional[str] = None

        self._init_client()
        self._initialized = True

    def _get_ist_date_str(self) -> str:
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        ist_now = now_utc + datetime.timedelta(hours=5, minutes=30)
        return ist_now.strftime("%Y-%m-%d")

    def _check_day_rollover(self):
        today_ist = self._get_ist_date_str()
        if today_ist != self.current_ist_date:
            self.current_ist_date = today_ist
            self.writes_today = 0
            self.reads_today = 0
            self.deletes_today = 0
            self.alerted_80 = False
            self.alerted_95 = False

    def _init_client(self):
        """Initializes Firebase Admin SDK from JSON credentials string."""
        if not self.credentials_json_str:
            logger.info("FirestoreBackend: No FIREBASE_CREDENTIALS_JSON found. Operating in LOCAL_ONLY mode.")
            self.mode = "LOCAL_ONLY"
            self.is_connected = False
            return

        try:
            import firebase_admin
            from firebase_admin import credentials, firestore

            cred_dict = json.loads(self.credentials_json_str)
            # Basic validation without leaking credentials
            if "project_id" not in cred_dict or "private_key" not in cred_dict:
                logger.error("FirestoreBackend: Malformed credentials JSON (missing required keys). Running in LOCAL_ONLY mode.")
                self.mode = "LOCAL_ONLY"
                self.is_connected = False
                return

            cred = credentials.Certificate(cred_dict)
            # Use unique app name or default
            app_name = f"finplus_{self.env_prefix}"
            try:
                app = firebase_admin.get_app(app_name)
            except ValueError:
                app = firebase_admin.initialize_app(cred, name=app_name)

            self.client = firestore.client(app=app)
            self.is_connected = True
            self.mode = f"CONNECTED ({self.env_prefix})"
            logger.info(f"FirestoreBackend initialized successfully for environment '{self.env_prefix}' (Project: {cred_dict.get('project_id')}).")
        except Exception as e:
            logger.error(f"FirestoreBackend initialization error: {e}")
            self.client = None
            self.is_connected = False
            self.mode = "ERROR"

    def can_write(self, is_critical: bool = False, is_paper_trade: bool = False) -> bool:
        """Enforces Free Spark Plan Quota Guard."""
        self._check_day_rollover()
        if not self.is_connected or not self.client:
            return False

        if is_paper_trade:
            # Paper trades can write up to 100%
            return self.writes_today < MAX_WRITES_PER_DAY

        if self.writes_today >= WARN_WRITES_95:
            # Over 95%: Only paper trades allowed
            return False

        if self.writes_today >= WARN_WRITES_80 and not is_critical:
            # Over 80%: Only critical writes allowed (paper trades, ledger, quota)
            return False

        return self.writes_today < MAX_WRITES_PER_DAY

    def record_usage(self, reads: int = 0, writes: int = 0, deletes: int = 0, dispatcher = None):
        """Records Firestore operation counts and triggers quota alerts at 80% and 95%."""
        self._check_day_rollover()
        self.reads_today += reads
        self.writes_today += writes
        self.deletes_today += deletes

        # 80% alert
        if (self.writes_today >= WARN_WRITES_80 or self.reads_today >= WARN_READS_80) and not self.alerted_80:
            self.alerted_80 = True
            logger.warning(
                f"FIRESTORE 80% QUOTA WARNING: Writes {self.writes_today}/{MAX_WRITES_PER_DAY}, "
                f"Reads {self.reads_today}/{MAX_READS_PER_DAY}. Non-essential sync paused."
            )
            if dispatcher and hasattr(dispatcher, "send_admin_alert"):
                dispatcher.send_admin_alert(
                    "Firestore 80% Quota Warning",
                    f"Writes: {self.writes_today}/{MAX_WRITES_PER_DAY} | Reads: {self.reads_today}/{MAX_READS_PER_DAY}"
                )

        # 95% alert
        if (self.writes_today >= WARN_WRITES_95 or self.reads_today >= WARN_READS_95) and not self.alerted_95:
            self.alerted_95 = True
            logger.critical(
                f"FIRESTORE 95% QUOTA ALERT: Writes {self.writes_today}/{MAX_WRITES_PER_DAY}, "
                f"Reads {self.reads_today}/{MAX_READS_PER_DAY}. Only paper trades permitted."
            )
            if dispatcher and hasattr(dispatcher, "send_admin_alert"):
                dispatcher.send_admin_alert(
                    "Firestore 95% Quota Emergency",
                    f"Writes: {self.writes_today}/{MAX_WRITES_PER_DAY}. All sync except paper trades blocked."
                )

    def get_collection_path(self, subcollection: str) -> str:
        """Returns environment-isolated path, e.g. 'env/live/subcollection'."""
        return f"env/{self.env_prefix}/{subcollection}"

    # -------------------------------------------------------------
    # 1. MIRRORING: Paper Trades & State
    # -------------------------------------------------------------
    def mirror_paper_position(self, pos: Dict[str, Any]):
        """Mirrors active or closed paper position to Firestore."""
        if not self.can_write(is_paper_trade=True):
            return

        try:
            pos_id = pos.get("position_id")
            if not pos_id:
                return
            col = self.get_collection_path("paper_trades")
            doc_ref = self.client.document(f"{col}/{pos_id}")
            doc_ref.set(pos, merge=True)
            self.record_usage(writes=1)
        except Exception as e:
            logger.warning(f"Firestore mirror paper trade error: {e}")

    def mirror_virtual_capital(self, capital: float):
        """Mirrors current virtual capital state."""
        if not self.can_write(is_critical=True):
            return
        try:
            col = self.get_collection_path("state")
            doc_ref = self.client.document(f"{col}/virtual_capital")
            doc_ref.set({"virtual_capital": capital, "updated_at_utc": get_utc_iso()}, merge=True)
            self.record_usage(writes=1)
        except Exception as e:
            logger.warning(f"Firestore mirror virtual capital error: {e}")

    def mirror_ledger_milestone(self, slot_id: str, date_str: str, record: Dict[str, Any]):
        """Mirrors schedule ledger milestone state."""
        if not self.can_write(is_critical=True):
            return
        try:
            col = self.get_collection_path("schedule_ledger")
            doc_ref = self.client.document(f"{col}/{date_str}_{slot_id}")
            doc_ref.set(record, merge=True)
            self.record_usage(writes=1)
        except Exception as e:
            logger.warning(f"Firestore mirror milestone error: {e}")

    def mirror_ntfy_quota(self, date_str: str, quota_count: int):
        """Mirrors daily NTFY quota count."""
        if not self.can_write(is_critical=True):
            return
        try:
            col = self.get_collection_path("state")
            doc_ref = self.client.document(f"{col}/ntfy_quota_{date_str}")
            doc_ref.set({"date_str": date_str, "quota_count": quota_count, "updated_at_utc": get_utc_iso()}, merge=True)
            self.record_usage(writes=1)
        except Exception as e:
            logger.warning(f"Firestore mirror ntfy quota error: {e}")

    def mirror_daily_latch(self, date_str: str, symbol: str, break_direction: int = 1, break_price: float = 0.0):
        """Mirrors daily breakout deduplication latch."""
        if not self.can_write(is_critical=True):
            return
        try:
            col = self.get_collection_path("latches")
            doc_ref = self.client.document(f"{col}/{date_str}_{symbol}")
            doc_ref.set({
                "date_str": date_str,
                "symbol": symbol,
                "break_direction": int(break_direction),
                "break_price": float(break_price),
                "timestamp_utc": get_utc_iso()
            }, merge=True)
            self.record_usage(writes=1)
        except Exception as e:
            logger.warning(f"Firestore mirror latch error: {e}")

    def mirror_setup_stats(self, setup_key: str, stats: Dict[str, Any]):
        """Mirrors setup stats and grade to Firestore."""
        if not self.can_write(is_critical=True):
            return
        try:
            col = self.get_collection_path("setup_stats")
            doc_ref = self.client.document(f"{col}/{setup_key}")
            doc_ref.set(stats, merge=True)
            self.record_usage(writes=1)
        except Exception as e:
            logger.warning(f"Firestore mirror setup stats error: {e}")

    def mirror_watch_pacing(self, date_str: str, pacing_data: Dict[str, Any]):
        """Mirrors watch pacing and symbols alerted today."""
        if not self.can_write(is_critical=True):
            return
        try:
            col = self.get_collection_path("state")
            doc_ref = self.client.document(f"{col}/watch_pacing_{date_str}")
            doc_ref.set(pacing_data, merge=True)
            self.record_usage(writes=1)
        except Exception as e:
            logger.warning(f"Firestore mirror watch pacing error: {e}")

    def mirror_yfinance_usage(self, date_str: str, usage_data: Dict[str, Any]):
        """Mirrors yfinance daily gateway usage counters."""
        if not self.can_write(is_critical=True):
            return
        try:
            col = self.get_collection_path("state")
            doc_ref = self.client.document(f"{col}/yfinance_usage_{date_str}")
            doc_ref.set(usage_data, merge=True)
            self.record_usage(writes=1)
        except Exception as e:
            logger.warning(f"Firestore mirror yfinance usage error: {e}")

    # -------------------------------------------------------------
    # 2. CANDLE HISTORY: One Compact Gzipped Document Per Symbol
    # -------------------------------------------------------------
    def upload_symbol_candles(self, symbol: str, timeframe: str, candles: List[Dict[str, Any]]) -> bool:
        """
        Compresses all candles of a symbol into ONE gzipped base64 document and uploads to Firestore.
        Format: [[epoch, open, high, low, close, volume], ...]
        Size: ~3-5 KB per symbol (easily stays well below Firestore's 1 MiB limit).
        """
        if not self.can_write(is_critical=False):
            return False

        if not candles:
            return False

        try:
            clean_sym = symbol.split(" ")[0].upper()
            compact_matrix = [
                [int(c.get("time", c.get("epoch", 0))), float(c.get("open", 0.0)), float(c.get("high", 0.0)),
                 float(c.get("low", 0.0)), float(c.get("close", 0.0)), int(c.get("volume", 0))]
                for c in candles
            ]
            raw_json = json.dumps(compact_matrix, separators=(',', ':'))
            compressed_bytes = gzip.compress(raw_json.encode('utf-8'))
            b64_str = base64.b64encode(compressed_bytes).decode('ascii')

            payload = {
                "symbol": clean_sym,
                "timeframe": timeframe,
                "bars_count": len(compact_matrix),
                "uncompressed_bytes": len(raw_json),
                "compressed_bytes": len(compressed_bytes),
                "updated_at_utc": get_utc_iso(),
                "data_gz_b64": b64_str
            }

            col = self.get_collection_path("candles")
            doc_ref = self.client.document(f"{col}/{clean_sym}_{timeframe}")
            doc_ref.set(payload)
            self.record_usage(writes=1)
            return True
        except Exception as e:
            logger.warning(f"Firestore upload candles error for {symbol}: {e}")
            return False

    def upload_all_candles_from_sqlite(self, candle_store, timeframe: str = "15m", limit: int = 1000) -> Dict[str, Any]:
        """Uploads full universe candle history from SQLite to Firestore (e.g. post-market 15:45 or after backfill)."""
        if not self.can_write(is_critical=False):
            return {"status": "BLOCKED_BY_QUOTA", "uploaded": 0}

        start_ts = time.time()
        with candle_store._get_connection() as conn:
            symbols = [r[0] for r in conn.execute("SELECT DISTINCT symbol FROM candles WHERE timeframe = ?", (timeframe,)).fetchall()]

        uploaded = 0
        total_bytes = 0
        for s in symbols:
            candles = candle_store.get_candles(s, timeframe=timeframe, limit=limit)
            if candles:
                if self.upload_symbol_candles(s, timeframe, candles):
                    uploaded += 1

        dur = time.time() - start_ts
        self.last_candle_sync_time = get_utc_iso()
        logger.info(f"Firestore candle sync completed: {uploaded}/{len(symbols)} symbols uploaded in {dur:.1f}s.")
        return {
            "status": "SUCCESS",
            "symbols_uploaded": uploaded,
            "total_symbols": len(symbols),
            "timestamp_utc": self.last_candle_sync_time,
            "duration_sec": round(dur, 2)
        }

    # -------------------------------------------------------------
    # 3. RESTORE AT STARTUP
    # -------------------------------------------------------------
    def restore_all_from_firestore(self, candle_store, paper_tracker = None, schedule_ledger = None, symbols_filter: Optional[Set[str]] = None) -> Dict[str, Any]:
        """
        Restores local SQLite databases from Firestore at cold startup.
        Pulls candles, active paper positions, schedule milestones, and quota.
        If symbols_filter is provided, only candles for those symbols are written to SQLite.
        """
        if not self.is_connected or not self.client:
            self.last_restore_reason = "Skipped: Firestore not connected (LOCAL_ONLY mode)"
            return {"status": "SKIPPED", "message": "Firestore not connected (LOCAL_ONLY mode)"}

        start_ts = time.time()
        restored_candles_count = 0
        restored_symbols = 0
        reads_used = 0

        logger.info(f"STARTUP RESTORE: Initiating data recovery from Firestore environment '{self.env_prefix}'...")

        # 1. Restore Candles (Per Symbol: Restore if Firestore has more bars than local SQLite)
        firestore_symbols_found = set()
        try:
            # Query local SQLite bar counts beforehand to compare per-symbol
            with candle_store._get_connection() as conn:
                local_rows = conn.execute("SELECT symbol, COUNT(*) FROM candles WHERE timeframe = '15m' GROUP BY symbol").fetchall()
            local_counts = {r[0].upper(): r[1] for r in local_rows}

            col = self.get_collection_path("candles")
            docs = self.client.collection(col).stream()
            for doc in docs:
                reads_used += 1
                data = doc.to_dict()
                sym = data.get("symbol")
                tf = data.get("timeframe", "15m")
                b64_str = data.get("data_gz_b64")
                fb_bars_count = data.get("bars_count", 0)
                
                if sym and b64_str:
                    clean_sym = sym.split(" ")[0].upper()
                    firestore_symbols_found.add(clean_sym)

                    if symbols_filter and clean_sym not in symbols_filter:
                        continue

                    local_cnt = local_counts.get(clean_sym, 0)
                    # Per-symbol rule: Restore if Firestore has more bars than local database, or local is short
                    if fb_bars_count > 0 and fb_bars_count <= local_cnt and local_cnt >= 522:
                        continue

                    try:
                        compressed = base64.b64decode(b64_str.encode('ascii'))
                        raw_json = gzip.decompress(compressed).decode('utf-8')
                        matrix = json.loads(raw_json)
                        
                        if len(matrix) <= local_cnt and local_cnt >= 522:
                            continue

                        # High-speed bulk insert into SQLite via executemany
                        records = []
                        for row in matrix:
                            epoch, o, h, l, c, v = row
                            utc_dt = datetime.datetime.fromtimestamp(epoch, tz=datetime.timezone.utc)
                            ist_dt = utc_dt + datetime.timedelta(hours=5, minutes=30)
                            records.append((
                                clean_sym, tf, int(epoch),
                                utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                ist_dt.strftime("%Y-%m-%d %H:%M:%S"),
                                float(o), float(h), float(l), float(c), int(v)
                            ))
                        with candle_store._get_connection() as conn:
                            conn.executemany("""
                                INSERT OR REPLACE INTO candles
                                (symbol, timeframe, epoch, timestamp_utc, timestamp_ist, open, high, low, close, volume)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """, records)
                            conn.commit()
                        restored_symbols += 1
                        restored_candles_count += len(matrix)
                        local_counts[clean_sym] = len(matrix)
                    except Exception as parse_err:
                        logger.warning(f"Error parsing candle document for {sym}: {parse_err}")
        except Exception as e:
            logger.error(f"Error restoring candles from Firestore: {e}")

        # 2. Restore Paper Trades
        restored_trades = 0
        if paper_tracker:
            try:
                col = self.get_collection_path("paper_trades")
                docs = self.client.collection(col).stream()
                for doc in docs:
                    reads_used += 1
                    pos = doc.to_dict()
                    pos_id = pos.get("position_id")
                    if pos_id:
                        status = pos.get("status", "OPEN")
                        with paper_tracker._get_connection() as conn:
                            if status == "OPEN":
                                conn.execute("""
                                    INSERT OR REPLACE INTO active_paper_positions
                                    (position_id, source_app, sector, symbol, action, is_option, is_put,
                                     entry_price, current_price, initial_sl, trailing_sl, target, quantity,
                                     risk_r, opt_entry_premium, opt_current_premium, opt_initial_sl,
                                     opt_trailing_sl, opt_target, stage, status, entry_time_ist, opened_at_utc,
                                     notes, highest_price, tags, setup_key, grade, taken, taken_time_ist,
                                     real_entry, real_qty, last_trail_ntfy_ts)
                                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                """, (
                                    pos_id, pos.get("source_app", "SCANNER"), pos.get("sector", "INTRADAY_CASH"),
                                    pos.get("symbol"), pos.get("action", "BUY"), pos.get("is_option", 0), pos.get("is_put", 0),
                                    pos.get("entry_price", 0.0), pos.get("current_price", 0.0), pos.get("initial_sl", 0.0),
                                    pos.get("trailing_sl", 0.0), pos.get("target", 0.0), pos.get("quantity", 1),
                                    pos.get("risk_r", 2.0), pos.get("opt_entry_premium", 0.0), pos.get("opt_current_premium", 0.0),
                                    pos.get("opt_initial_sl", 0.0), pos.get("opt_trailing_sl", 0.0), pos.get("opt_target", 0.0),
                                    pos.get("stage", "INITIAL"), "OPEN", pos.get("entry_time_ist", ""), pos.get("opened_at_utc", ""),
                                    pos.get("notes", ""), pos.get("highest_price", 0.0), pos.get("tags", ""),
                                    pos.get("setup_key", ""), pos.get("grade", "B"), pos.get("taken", 0),
                                    pos.get("taken_time_ist", ""), pos.get("real_entry", 0.0), pos.get("real_qty", 0),
                                    pos.get("last_trail_ntfy_ts", 0.0)
                                ))
                                paper_tracker.active_positions[pos_id] = pos
                            else:
                                trade_id = pos.get("trade_id", f"TRD_{pos_id}")
                                conn.execute("""
                                    INSERT OR REPLACE INTO closed_paper_trades
                                    (trade_id, position_id, symbol, sector, action, is_option, entry_price,
                                     exit_price, quantity, gross_pnl, total_charges, net_pnl, exit_reason,
                                     entry_time_ist, exit_time_ist, opened_at_utc, closed_at_utc,
                                     highest_price, tags, setup_key, grade, taken, taken_time_ist, real_entry, real_qty, r_multiple)
                                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                """, (
                                    trade_id, pos_id, pos.get("symbol"), pos.get("sector", "INTRADAY_CASH"),
                                    pos.get("action", "BUY"), pos.get("is_option", 0), pos.get("entry_price", 0.0),
                                    pos.get("exit_price", 0.0), pos.get("quantity", 1), pos.get("gross_pnl", 0.0),
                                    pos.get("total_charges", 0.0), pos.get("net_pnl", 0.0), pos.get("exit_reason", "EXIT"),
                                    pos.get("entry_time_ist", ""), pos.get("exit_time_ist", ""), pos.get("opened_at_utc", ""),
                                    pos.get("closed_at_utc", ""), pos.get("highest_price", 0.0), pos.get("tags", ""),
                                    pos.get("setup_key", ""), pos.get("grade", "B"), pos.get("taken", 0),
                                    pos.get("taken_time_ist", ""), pos.get("real_entry", 0.0), pos.get("real_qty", 0),
                                    pos.get("r_multiple", 0.0)
                                ))
                            conn.commit()
                        restored_trades += 1
            except Exception as e:
                logger.error(f"Error restoring paper trades from Firestore: {e}")

        # 3. Restore Setup Stats
        if paper_tracker:
            try:
                col = self.get_collection_path("setup_stats")
                docs = self.client.collection(col).stream()
                for doc in docs:
                    reads_used += 1
                    data = doc.to_dict()
                    sk = data.get("setup_key")
                    if sk:
                        with paper_tracker._get_connection() as conn:
                            conn.execute("""
                                INSERT OR REPLACE INTO setup_stats (
                                    setup_key, total_trades, wins, win_pct, avg_r, expectancy,
                                    rolling_30_trades, rolling_30_wins, rolling_30_win_pct, rolling_30_expectancy,
                                    grade, updated_at_utc
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """, (
                                sk, data.get("total_trades", 0), data.get("wins", 0), data.get("win_pct", 0.0),
                                data.get("avg_r", 0.0), data.get("expectancy", 0.0),
                                data.get("rolling_30_trades", 0), data.get("rolling_30_wins", 0),
                                data.get("rolling_30_win_pct", 0.0), data.get("rolling_30_expectancy", 0.0),
                                data.get("grade", "B"), data.get("updated_at_utc", get_utc_iso())
                            ))
                            conn.commit()
            except Exception as e:
                logger.warning(f"Error restoring setup stats from Firestore: {e}")

        # 4. Restore Daily Latches
        if schedule_ledger:
            try:
                col = self.get_collection_path("latches")
                docs = self.client.collection(col).stream()
                for doc in docs:
                    reads_used += 1
                    data = doc.to_dict()
                    d_str = data.get("date_str")
                    sym = data.get("symbol")
                    if d_str and sym:
                        with schedule_ledger._get_connection() as conn:
                            conn.execute("""
                                INSERT OR IGNORE INTO daily_breakout_latch (date_str, symbol, break_direction, break_price, latched_at_utc)
                                VALUES (?, ?, ?, ?, ?)
                            """, (
                                d_str, sym, int(data.get("break_direction", 1)),
                                float(data.get("break_price", 0.0)),
                                data.get("timestamp_utc", get_utc_iso())
                            ))
                            conn.commit()
            except Exception as e:
                logger.warning(f"Error restoring latches from Firestore: {e}")

        # 5. Restore Schedule Ledger Milestones
        if schedule_ledger:
            try:
                col = self.get_collection_path("schedule_ledger")
                docs = self.client.collection(col).stream()
                for doc in docs:
                    reads_used += 1
                    data = doc.to_dict()
                    d_str = data.get("date_str")
                    s_id = data.get("slot_id")
                    if d_str and s_id:
                        with schedule_ledger._get_connection() as conn:
                            conn.execute("""
                                INSERT OR REPLACE INTO daily_schedule_ledger (
                                    date_str, slot_id, status, executed_at_utc, scheduled_time_ist,
                                    dispatched_at_ist, attempts, error_message, updated_at_utc
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """, (
                                d_str, s_id, data.get("status", "SUCCESS"), data.get("executed_at_utc", ""),
                                data.get("scheduled_time_ist", ""), data.get("dispatched_at_ist", ""),
                                data.get("attempts", 1), data.get("error_message", None),
                                data.get("updated_at_utc", get_utc_iso())
                            ))
                            conn.commit()
            except Exception as e:
                logger.warning(f"Error restoring schedule ledger from Firestore: {e}")

        # 6. Restore Virtual Capital & State
        try:
            col = self.get_collection_path("state")
            # Virtual Capital
            vc_doc = self.client.document(f"{col}/virtual_capital").get()
            reads_used += 1
            if vc_doc.exists:
                vc_data = vc_doc.to_dict()
                if paper_tracker and "virtual_capital" in vc_data:
                    paper_tracker.virtual_capital = float(vc_data["virtual_capital"])
                    logger.info(f"Restored virtual capital: ₹{paper_tracker.virtual_capital:,.2f}")

            # NTFY Quota for today
            today_str = self._get_ist_date_str()
            nq_doc = self.client.document(f"{col}/ntfy_quota_{today_str}").get()
            reads_used += 1
            if nq_doc.exists:
                nq_data = nq_doc.to_dict()
                cnt = nq_data.get("quota_count", 0)
                if paper_tracker:
                    paper_tracker.daily_ntfy_triggers_count = cnt
                    logger.info(f"Restored daily NTFY quota count for {today_str}: {cnt}")
        except Exception as e:
            logger.warning(f"Error restoring state/virtual capital from Firestore: {e}")

        # Record total reads used during restore
        self.record_usage(reads=reads_used)
        dur = time.time() - start_ts
        restore_res = {
            "source": "firestore",
            "status": "SUCCESS",
            "symbols_restored": restored_symbols,
            "bars_restored": restored_candles_count,
            "trades_restored": restored_trades,
            "reads_used": reads_used,
            "duration_sec": round(dur, 2),
            "firestore_symbols": list(firestore_symbols_found),
            "timestamp_utc": get_utc_iso()
        }
        self.last_restore_result = restore_res
        self.last_restore_reason = (
            f"Restored {restored_symbols} symbols ({restored_candles_count} bars), "
            f"{restored_trades} paper trades from Firestore in {dur:.2f}s"
        )
        logger.info(
            f"STARTUP RESTORE FINISHED: {restored_symbols} symbols ({restored_candles_count} bars), "
            f"{restored_trades} paper trades restored in {dur:.2f}s (Reads used: {reads_used})."
        )

        return restore_res

    def restore_per_symbol(self, candle_store, tracked_symbols = None, paper_tracker = None, schedule_ledger = None) -> Dict[str, Any]:
        """Convenience method for per-symbol restore comparison against Firestore."""
        sym_filter = set(tracked_symbols) if tracked_symbols else None
        return self.restore_all_from_firestore(candle_store, paper_tracker, schedule_ledger, symbols_filter=sym_filter)

    # -------------------------------------------------------------
    # 4. TELEMETRY & HEALTH
    # -------------------------------------------------------------
    def get_telemetry(self) -> Dict[str, Any]:
        """Returns Firestore connection, restore history, sync time, and daily quota usage telemetry."""
        self._check_day_rollover()
        return {
            "status": self.mode,
            "is_connected": self.is_connected,
            "env_prefix": self.env_prefix,
            "date_ist": self.current_ist_date,
            "last_restore": self.last_restore_result or {"source": "none", "status": "NO_RESTORE_RUN"},
            "last_restore_reason": self.last_restore_reason,
            "last_candle_sync_time": self.last_candle_sync_time,
            "reads_today": self.reads_today,
            "max_reads_per_day": MAX_READS_PER_DAY,
            "writes_today": self.writes_today,
            "max_writes_per_day": MAX_WRITES_PER_DAY,
            "deletes_today": self.deletes_today,
            "max_deletes_per_day": MAX_DELETES_PER_DAY,
            "writes_remaining": max(0, MAX_WRITES_PER_DAY - self.writes_today),
            "reads_remaining": max(0, MAX_READS_PER_DAY - self.reads_today),
            "quota_status": (
                "EMERGENCY_95" if self.writes_today >= WARN_WRITES_95 or self.reads_today >= WARN_READS_95
                else ("WARNING_80" if self.writes_today >= WARN_WRITES_80 or self.reads_today >= WARN_READS_80 else "HEALTHY")
            )
        }

